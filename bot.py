import os
import csv
import io
import json
import time
import asyncio

import aiohttp
import discord
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
CHANNEL_ID = int(os.getenv('CHANNEL_ID'))
SHEET_CSV_URL = os.getenv('SHEET_CSV_URL')
ROLE_ID = os.getenv('ROLE_ID', '').strip()
BACKFILL_DAYS = int(os.getenv('BACKFILL_DAYS', '0') or 0)

DATA_FILE = 'data.json'
ANILIST_URL = 'https://graphql.anilist.co'
CHECK_MINUTES = 10
RECHECK_HOURS = 6
MAX_CATCHUP_DAYS = 7

FIND_QUERY = '''
query ($search: String) {
  Media(search: $search, type: ANIME, status_in: [RELEASING, NOT_YET_RELEASED]) {
    id
    title { romaji english }
  }
}
'''

AIRING_QUERY = '''
query ($ids: [Int], $from: Int, $to: Int) {
  Page(perPage: 50) {
    airingSchedules(mediaId_in: $ids, airingAt_greater: $from, airingAt_lesser: $to) {
      id
      episode
      airingAt
      media {
        id
        siteUrl
        bannerImage
        coverImage { extraLarge color }
        externalLinks { site url type }
        nextAiringEpisode { airingAt episode }
        title { romaji english }
      }
    }
  }
}
'''


def load_data():
    try:
        with open(DATA_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    data.setdefault('titles', {})
    data.setdefault('posted', [])
    data.setdefault('last_check', 0)
    data.setdefault('summary_sent', False)
    return data


async def anilist(session, query, variables):
    payload = {'query': query, 'variables': variables}
    for attempt in range(3):
        async with session.post(ANILIST_URL, json=payload) as r:
            if r.status == 200:
                js = await r.json()
                return 200, js.get('data')
            if r.status == 429:
                try:
                    wait = int(r.headers.get('Retry-After', '60'))
                except ValueError:
                    wait = 60
                wait = min(max(wait, 5), 90)
                print(f'AniList is rate limiting us. Waiting {wait} seconds...')
                await asyncio.sleep(wait)
                continue
            return r.status, None
    return 429, None


class AnimeBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.session = None
        self.data = load_data()
        if BACKFILL_DAYS > 0:
            self.data['last_check'] = int(time.time()) - BACKFILL_DAYS * 86400
            self.data['posted'] = []

    def save(self):
        with open(DATA_FILE, 'w') as f:
            json.dump(self.data, f, indent=2)

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.check_anime.start()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        print(f'Logged in as {self.user}')
        if self.get_channel(CHANNEL_ID) is None:
            print('WARNING: cannot find the channel. Check CHANNEL_ID and permissions.')

    async def read_sheet(self):
        async with self.session.get(SHEET_CSV_URL) as r:
            r.raise_for_status()
            text = await r.text()
        text = text.lstrip('\ufeff')
        shows = []
        for raw in csv.DictReader(io.StringIO(text)):
            row = {k.strip().lower(): (v or '').strip() for k, v in raw.items() if k}
            if 'anime' not in row.get('type', '').lower():
                continue
            title = row.get('title')
            if title:
                shows.append((title, row.get('message', '')))
        return shows

    async def resolve(self, title):
        key = title.lower()
        entry = self.data['titles'].get(key)
        now = int(time.time())
        if entry and now - entry['checked'] < RECHECK_HOURS * 3600:
            return
        status, data = await anilist(self.session, FIND_QUERY, {'search': title})
        if status == 200 and data and data.get('Media'):
            media = data['Media']
            t = media['title']
            name = t.get('english') or t.get('romaji') or title
            self.data['titles'][key] = {'id': media['id'], 'name': name, 'checked': now}
        elif status == 404:
            self.data['titles'][key] = {'id': None, 'name': None, 'checked': now}
        else:
            print(f'AniList lookup problem for {title} (status {status})')
        await asyncio.sleep(2.5)

    async def send_summary(self, channel, shows):
        if self.data.get('summary_sent'):
            return
        if any(self.data['titles'].get(t.lower()) is None for t, _ in shows):
            return
        matched = []
        skipped = []
        for title, _ in shows:
            entry = self.data['titles'][title.lower()]
            if entry['id']:
                matched.append(f"• {title} → {entry['name']}")
            else:
                skipped.append(f'• {title}')
        text = '**Following (airing or upcoming)**\n' + ('\n'.join(matched) or 'None')
        text += '\n\n**Not airing right now (no posts)**\n' + ('\n'.join(skipped) or 'None')
        embed = discord.Embed(
            title='📺 Anime tracker started',
            description=text[:4000],
            colour=discord.Colour.blurple(),
        )
        await channel.send(embed=embed)
        self.data['summary_sent'] = True

    def make_card(self, s, message):
        media = s['media']
        t = media['title']
        name = t.get('english') or t.get('romaji') or 'Unknown'
        ep = s['episode']
        cover = media.get('coverImage') or {}

        colour = discord.Colour.blurple()
        hex_colour = cover.get('color')
        if hex_colour:
            try:
                colour = discord.Colour(int(hex_colour.lstrip('#'), 16))
            except ValueError:
                pass

        lines = []
        if message:
            lines.append(message)
        link = media['siteUrl']
        label = 'VIEW ON ANILIST'
        site = ''
        for lk in media.get('externalLinks') or []:
            if lk.get('type') == 'STREAMING' and lk.get('url'):
                site = lk.get('site') or ''
                link = lk['url']
                label = 'WATCH IT HERE!'
                break
        if site:
            lines.append(f'**{site}**')
        lines.append(f'[**{label}**]({link})')
        nxt = media.get('nextAiringEpisode')
        if nxt:
            lines.append(f"Next episode: <t:{nxt['airingAt']}:D>")

        embed = discord.Embed(
            title=f'{name} Episode {ep} has released!',
            description='\n'.join(lines),
            colour=colour,
            url=media['siteUrl'],
        )
        if cover.get('extraLarge'):
            embed.set_thumbnail(url=cover['extraLarge'])
        if media.get('bannerImage'):
            embed.set_image(url=media['bannerImage'])
        return embed

    @tasks.loop(minutes=CHECK_MINUTES)
    async def check_anime(self):
        channel = self.get_channel(CHANNEL_ID)
        if channel is None or not SHEET_CSV_URL:
            return
        try:
            shows = await self.read_sheet()
        except Exception as e:
            print(f'Sheet check failed: {e}')
            return

        for title, _ in shows:
            await self.resolve(title)
        self.save()
        await self.send_summary(channel, shows)

        messages = {}
        for title, msg in shows:
            entry = self.data['titles'].get(title.lower())
            if entry and entry['id']:
                messages[entry['id']] = msg
        ids = list(messages)

        now = int(time.time())
        if not self.data['last_check']:
            self.data['last_check'] = now
        if not ids:
            self.data['last_check'] = now
            self.save()
            return

        start = max(self.data['last_check'], now - MAX_CATCHUP_DAYS * 86400)
        status, data = await anilist(
            self.session, AIRING_QUERY, {'ids': ids, 'from': start, 'to': now}
        )
        if status != 200 or not data:
            print(f'Airing check failed (status {status})')
            return

        schedules = sorted(data['Page']['airingSchedules'], key=lambda s: s['airingAt'])
        for s in schedules:
            if s['id'] in self.data['posted']:
                continue
            embed = self.make_card(s, messages.get(s['media']['id'], ''))
            mention = f'<@&{ROLE_ID}>' if ROLE_ID else None
            try:
                await channel.send(content=mention, embed=embed)
            except Exception as e:
                print(f'Could not post: {e}')
                continue
            self.data['posted'].append(s['id'])

        self.data['last_check'] = now
        self.data['posted'] = self.data['posted'][-500:]
        self.save()

    @check_anime.before_loop
    async def before_check(self):
        await self.wait_until_ready()


client = AnimeBot()
client.run(TOKEN)

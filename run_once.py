import asyncio

import bot as b


class OneShot(b.AnimeBot):
    async def setup_hook(self):
        self.session = b.aiohttp.ClientSession()

    def get_channel(self, channel_id):
        return self.fixed_channel


async def main():
    client = OneShot()
    try:
        await client.login(b.TOKEN)
        client.fixed_channel = await client.fetch_channel(b.CHANNEL_ID)
        await client.check_anime()
    finally:
        await client.close()


asyncio.run(main())

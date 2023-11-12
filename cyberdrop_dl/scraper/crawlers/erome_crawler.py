from __future__ import annotations

from dataclasses import field
from typing import TYPE_CHECKING

from aiolimiter import AsyncLimiter
from yarl import URL

from cyberdrop_dl.utils.dataclasses.url_objects import MediaItem, ScrapeItem
from cyberdrop_dl.utils.utilities import get_filename_and_ext, error_handling_wrapper, log, get_download_path, remove_id

if TYPE_CHECKING:
    from asyncio import Queue

    from cyberdrop_dl.clients.scraper_client import ScraperClient
    from cyberdrop_dl.managers.manager import Manager


class EromeCrawler:
    def __init__(self, manager: Manager):
        self.manager = manager
        self.scraping_progress = manager.progress_manager.scraping_progress
        self.client: ScraperClient = field(init=False)

        self.complete = False

        self.scraped_items: list = []
        self.scraper_queue: Queue = field(init=False)
        self.download_queue: Queue = field(init=False)

        self.request_limiter = AsyncLimiter(10, 1)

    async def startup(self) -> None:
        """Starts the crawler"""
        self.scraper_queue = await self.manager.queue_manager.get_scraper_queue("erome")
        self.download_queue = await self.manager.queue_manager.get_download_queue("erome")

        self.client = self.manager.client_manager.scraper_session

    async def finish_task(self):
        self.scraper_queue.task_done()
        if self.scraper_queue.empty():
            self.complete = True

    async def run_loop(self) -> None:
        """Runs the crawler loop"""
        while True:
            item: ScrapeItem = await self.scraper_queue.get()
            await log(f"Scrape Starting: {item.url}")
            if item.url in self.scraped_items:
                await self.finish_task()
                continue

            self.complete = False
            self.scraped_items.append(item.url)
            await self.fetch(item)

            await log(f"Scrape Finished: {item.url}")
            await self.finish_task()

    async def fetch(self, scrape_item: ScrapeItem) -> None:
        """Determines where to send the scrape item based on the url"""
        task_id = await self.scraping_progress.add_task(scrape_item.url)

        if scrape_item.url.path.startswith("/a/"):
            await self.album(scrape_item)
        else:
            await self.profile(scrape_item)

        await self.scraping_progress.remove_task(task_id)

    @error_handling_wrapper
    async def profile(self, scrape_item: ScrapeItem) -> None:
        """Scrapes a profile"""
        async with self.request_limiter:
            soup = await self.client.get_BS4("erome", scrape_item.url)

        title = scrape_item.url.name + f" ({scrape_item.url.host})"
        albums = soup.select('a[class=album-link]')

        for album in albums:
            link = URL(album['href'])
            new_scrape_item = ScrapeItem(url=link, parent_title=title, part_of_album=True)
            await new_scrape_item.add_to_parent_title(title)
            await self.scraper_queue.put(new_scrape_item)

        next_page = soup.select_one('a[rel="next"]')
        if next_page:
            next_page = next_page.get("href").split("page=")[-1]
            new_scrape_item = ScrapeItem(url=scrape_item.url.with_query(f"page={next_page}"), parent_title=scrape_item.parent_title)
            await self.scraper_queue.put(new_scrape_item)

    @error_handling_wrapper
    async def album(self, scrape_item: ScrapeItem) -> None:
        """Scrapes an album"""
        async with self.request_limiter:
            soup = await self.client.get_BS4("erome", scrape_item.url)

        title = soup.select_one('div[class="col-sm-12 page-content"] h1').get_text()
        if not title:
            title = scrape_item.url.name
        await scrape_item.add_to_parent_title(title)

        # Images
        images = soup.select('img[class="img-front lasyload"]')
        vidoes = soup.select('div[class=media-group] div[class=video-lg] video source')

        for image in images:
            link = URL(image['data-src'])
            filename, ext = await get_filename_and_ext(link.name)
            await self.handle_file(link, scrape_item, filename, ext)

        for video in vidoes:
            link = URL(video['src'])
            filename, ext = await get_filename_and_ext(link.name)
            await self.handle_file(link, scrape_item, filename, ext)

    async def handle_file(self, url: URL, scrape_item: ScrapeItem, filename: str, ext: str) -> None:
        """Finishes handling the file and hands it off to the download_queue"""
        original_filename, filename = await remove_id(self.manager, filename, ext)

        check_complete = await self.manager.db_manager.history_table.check_complete("erome", url)
        if check_complete:
            await log(f"Skipping {url} as it has already been downloaded")
            await self.manager.progress_manager.download_progress.add_previously_completed()
            return

        download_folder = await get_download_path(self.manager, scrape_item, "Erome")
        media_item = MediaItem(url, scrape_item.url, download_folder, filename, ext, original_filename)
        if scrape_item.possible_datetime:
            media_item.datetime = scrape_item.possible_datetime

        await self.download_queue.put(media_item)

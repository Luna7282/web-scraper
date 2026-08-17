import asyncio
from urllib.parse import urljoin
from crawl4ai import AsyncWebCrawler
from rich.progress import Progress
from output_manager import OutputManager

async def worker(queue: asyncio.Queue, visited: set, crawler: AsyncWebCrawler, output_manager: OutputManager, allowed_branches: list[str], progress: Progress, task_id):
    while True:
        try:
            url = await queue.get()
            
            if url in visited:
                queue.task_done()
                continue
                
            visited.add(url)
            
            result = await crawler.arun(url=url)
            markdown = result.markdown
            
            if markdown:
                await output_manager.process_page(url, markdown)
            
            progress.update(task_id, advance=1)
            
            links = result.links.get("internal", []) + result.links.get("external", [])
            for link_obj in links:
                href = link_obj.get("href", "")
                if not href:
                    continue
                    
                if not href.startswith("http"):
                    href = urljoin(url, href)
                    
                if not href.startswith("http"):
                    continue
                    
                href = href.split('#')[0]
                
                if href not in visited:
                    if allowed_branches and "all" not in [b.lower() for b in allowed_branches]:
                        is_allowed = False
                        for branch in allowed_branches:
                            if href.startswith(branch):
                                is_allowed = True
                                break
                        if not is_allowed:
                            continue
                            
                    await queue.put(href)
                    
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Error processing {url}: {e}")
        finally:
            queue.task_done()

async def start_workers(start_urls: list[str], allowed_branches: list[str], output_manager: OutputManager, max_concurrent: int = 5):
    queue = asyncio.Queue()
    visited = set()
    
    for url in start_urls:
        await queue.put(url)
        
    with Progress() as progress:
        task_id = progress.add_task("[green]Crawling and Processing URLs...", total=None)
        
        async with AsyncWebCrawler() as crawler:
            workers = [
                asyncio.create_task(worker(queue, visited, crawler, output_manager, allowed_branches, progress, task_id))
                for _ in range(max_concurrent)
            ]
            
            await queue.join()
            
            for w in workers:
                w.cancel()
                
            await asyncio.gather(*workers, return_exceptions=True)
            
    print(f"\nCompleted crawling. Total unique URLs visited: {len(visited)}")

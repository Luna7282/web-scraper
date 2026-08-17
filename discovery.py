import asyncio
from urllib.parse import urlparse, urljoin
from crawl4ai import AsyncWebCrawler

async def discover_branches(root_url: str):
    print(f"Discovering branches from {root_url}...")
    try:
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=root_url)
            
            links = result.links.get("internal", []) + result.links.get("external", [])
            
            branches = {}
            for link_obj in links:
                href = link_obj.get("href", "")
                if not href:
                    continue
                    
                if not href.startswith("http"):
                    href = urljoin(root_url, href)
                
                if not href.startswith("http"):
                    continue
                    
                parsed = urlparse(href)
                domain = parsed.netloc
                path_parts = [p for p in parsed.path.split('/') if p]
                
                if domain != urlparse(root_url).netloc:
                    category = f"External: {domain}"
                else:
                    if path_parts:
                        category = f"Path: /{path_parts[0]}/*"
                    else:
                        category = "Root Level"
                        
                if category not in branches:
                    branches[category] = set()
                branches[category].add(href)
                
            return {k: list(v) for k, v in branches.items()}
            
    except Exception as e:
        print(f"Discovery failed: {e}")
        return {}

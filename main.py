import asyncio
import sys
from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.panel import Panel
from rich.table import Table

from config import OutputPreferences
from llm_factory import get_llm
from output_manager import OutputManager
from discovery import discover_branches
from orchestrator import start_workers

console = Console()

async def main():
    console.print(Panel.fit("[bold blue]Recursive Web Scraper with LLM & RAG[/bold blue]"))
    
    root_url = Prompt.ask("Enter the Root URL to start discovery")
    
    if not root_url.startswith("http"):
        root_url = "https://" + root_url
        
    branches = await discover_branches(root_url)
    
    if not branches:
        console.print("[red]No branches discovered or discovery failed.[/red]")
        sys.exit(1)
        
    table = Table(title="Discovered Branches")
    table.add_column("ID", style="cyan")
    table.add_column("Category", style="magenta")
    table.add_column("Count", justify="right", style="green")
    
    branch_list = list(branches.items())
    for i, (cat, urls) in enumerate(branch_list, 1):
        table.add_row(str(i), cat, str(len(urls)))
        
    console.print(table)
    
    branch_input = Prompt.ask("Enter comma-separated IDs of branches to crawl, or 'all'")
    
    selected_urls = []
    allowed_branch_prefixes = []
    
    if branch_input.strip().lower() == "all":
        for urls in branches.values():
            selected_urls.extend(urls)
        allowed_branch_prefixes = ["all"]
    else:
        try:
            ids = [int(x.strip()) for x in branch_input.split(',')]
            for i in ids:
                if 1 <= i <= len(branch_list):
                    cat, urls = branch_list[i-1]
                    selected_urls.extend(urls)
                    if urls:
                        allowed_branch_prefixes.extend(urls)
        except ValueError:
            console.print("[red]Invalid input. Exiting.[/red]")
            sys.exit(1)
            
    if not selected_urls:
        console.print("[yellow]No URLs selected. Exiting.[/yellow]")
        sys.exit(0)
        
    console.print("\n[bold]Select LLM Provider[/bold]")
    console.print("1. NVIDIA NIM")
    console.print("2. Ollama")
    console.print("3. OpenAI")
    
    llm_choice = Prompt.ask("Choice", choices=["1", "2", "3"], default="3")
    
    provider_map = {"1": "nvidia", "2": "ollama", "3": "openai"}
    provider_name = provider_map[llm_choice]
    
    default_model = "gpt-4o-mini"
    if provider_name == "nvidia":
        default_model = "meta/llama3-70b-instruct"
    elif provider_name == "ollama":
        default_model = "llama3"
        
    model_name = Prompt.ask(f"Enter model name for {provider_name}", default=default_model)
    
    llm, embeddings = get_llm(provider_name, model_name)
    
    console.print("\n[bold]Select Output Formats[/bold]")
    build_rag = Confirm.ask("Build Vector RAG (Chroma)?", default=False)
    unified_jsonl = Confirm.ask("Generate Unified JSONL?", default=True)
    split_jsonl = Confirm.ask("Generate Split JSONL (by section)?", default=False)
    
    prefs = OutputPreferences(
        unified_jsonl=unified_jsonl,
        split_jsonl=split_jsonl,
        build_rag=build_rag
    )
    
    output_manager = OutputManager(prefs, llm, embeddings)
    
    max_workers = Prompt.ask("Enter max concurrent workers", default="5")
    try:
        max_workers_int = int(max_workers)
    except:
        max_workers_int = 5
        
    console.print(f"\n[bold green]Starting scraping with {max_workers_int} workers...[/bold green]")
    await start_workers(selected_urls, allowed_branch_prefixes, output_manager, max_concurrent=max_workers_int)
    
    console.print("[bold blue]Done![/bold blue]")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")

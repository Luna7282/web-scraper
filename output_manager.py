import os
import json
import asyncio
import aiofiles
from urllib.parse import urlparse
from pathlib import Path
from pydantic import BaseModel

# Notice: All legacy retrievers and InMemoryStore imports are GONE
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_core.documents import Document

class QAPair(BaseModel):
    instruction: str
    response: str

class OutputManager:
    def __init__(self, prefs, llm, embeddings):
        self.prefs = prefs
        self.llm = llm
        self.embeddings = embeddings

        self.data_dir = "data"
        os.makedirs(self.data_dir, exist_ok=True)

        self.unified_path = os.path.join(self.data_dir, "unified.jsonl")

        # Async lock for thread-safe deduplication and file writes
        self.lock = asyncio.Lock()
        self.seen_instructions: set[str] = set()
        self._preload_existing_instructions()

        if self.prefs.build_rag:
            self.chroma_client = Chroma(
                collection_name="scraper_docs",
                embedding_function=self.embeddings,
                persist_directory="./chroma_db",
            )
            self.parent_splitter = RecursiveCharacterTextSplitter(chunk_size=2000)
            self.child_splitter = RecursiveCharacterTextSplitter(chunk_size=400)

        self.system_prompt = """You are an expert training data generator. Your task is to read the provided text and generate 3 to 5 diverse, high-quality instruction-response pairs for fine-tuning a Large Language Model.

CRITICAL RULES:
1. The 'instruction' must be a specific question or command that a user would realistically ask.
2. The 'instruction' MUST be entirely self-contained. Never use pronouns like "he", "it", or "this company".
3. The 'response' must be accurate, detailed, and derived ONLY from the provided text.
4. If the text is just a navigation menu or footer with no real content, return an empty list.

Format your output EXACTLY as a JSON array of objects, where each object has an "instruction" key and a "response" key."""

    def _preload_existing_instructions(self):
        """Scans existing JSONL files to prevent duplicate questions across runs."""
        for jsonl_file in Path(self.data_dir).glob("*.jsonl"):
            try:
                with open(jsonl_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            data = json.loads(line)
                            inst = data.get("instruction", "").strip().lower()
                            if inst:
                                self.seen_instructions.add(inst)
            except Exception as e:
                print(f"Warning: Could not pre-read {jsonl_file}: {e}")

    async def _generate_qa(self, markdown: str) -> list[QAPair]:
        if not markdown or len(markdown.strip()) < 50:
            return []

        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=f"Text to process:\n\n{markdown[:4000]}"),
        ]

        try:
            response = await asyncio.to_thread(self.llm.invoke, messages)
            content = response.content.strip()
            if content.startswith("```json"):
                content = content[7:-3]
            elif content.startswith("```"):
                content = content[3:-3]

            pairs = json.loads(content)
            if not isinstance(pairs, list):
                return []

            return [
                QAPair(**p)
                for p in pairs
                if isinstance(p, dict) and "instruction" in p and "response" in p
            ]
        except Exception as e:
            print(f"Failed to generate/parse QA: {e}")
            return []

    async def process_page(self, url: str, markdown: str):
        tasks = []

        if self.prefs.unified_jsonl or self.prefs.split_jsonl:
            pairs = await self._generate_qa(markdown)

            if pairs:
                unique_pairs = []
                async with self.lock:
                    for p in pairs:
                        norm_inst = p.instruction.strip().lower()
                        if norm_inst not in self.seen_instructions:
                            self.seen_instructions.add(norm_inst)
                            unique_pairs.append(p)

                if unique_pairs:
                    json_lines = [
                        json.dumps({"instruction": p.instruction, "response": p.response})
                        + "\n"
                        for p in unique_pairs
                    ]

                    if self.prefs.unified_jsonl:
                        tasks.append(self._write_lines(self.unified_path, json_lines))

                    if self.prefs.split_jsonl:
                        parsed_url = urlparse(url)
                        path_parts = [p for p in parsed_url.path.split("/") if p]
                        section = path_parts[0] if path_parts else "root"
                        section = "".join(
                            c for c in section if c.isalnum() or c in ("-", "_")
                        )
                        if not section:
                            section = "root"
                        split_path = os.path.join(self.data_dir, f"{section}.jsonl")
                        tasks.append(self._write_lines(split_path, json_lines))

        if self.prefs.build_rag and markdown:
            tasks.append(asyncio.to_thread(self._add_to_rag, url, markdown))

        if tasks:
            await asyncio.gather(*tasks)

    async def _write_lines(self, path: str, lines: list[str]):
        async with self.lock:
            async with aiofiles.open(path, mode="a", encoding="utf-8") as f:
                await f.writelines(lines)

    def _add_to_rag(self, url: str, markdown: str):
        """Custom, persistent Parent-Child Retrieval Logic"""
        try:
            # 1. Split the page into large Parent Chunks
            parent_chunks = self.parent_splitter.split_text(markdown)
            
            child_docs = []
            
            for parent_text in parent_chunks:
                # 2. Break the parent down into tiny Child Chunks
                children = self.child_splitter.split_text(parent_text)
                
                for child in children:
                    # 3. Store the child for searching, but attach the ENTIRE parent text to its metadata!
                    child_docs.append(
                        Document(
                            page_content=child, 
                            metadata={
                                "source": url, 
                                "parent_text": parent_text # Full context saved natively!
                            }
                        )
                    )
            
            # 4. Add to Vector DB
            if child_docs:
                self.chroma_client.add_documents(child_docs)
                
        except Exception as e:
            print(f"Failed to add to RAG for {url}: {e}")
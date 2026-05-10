"""
Aviator (SM8845) NPI Engineering RAG System

Stages:
  1. PDF ingestion → FAISS vector store (HuggingFace embeddings)
  2. Parametric shift detection vs golden sample CSV (>5 % threshold)
  3. RAG-based RCA / control-plan queries via Claude claude-opus-4-7

Caching strategy:
  - The system prompt (stable persona + instructions) carries cache_control.
  - For Opus 4.7 the minimum cacheable prefix is 4096 tokens; the bare system
    prompt below won't hit that alone.  Embed additional static reference
    material (common torque specs, failure codes, process notes) into
    _SYSTEM_PROMPT to grow the prefix into cacheable range and get ~90 % cost
    reduction on the persona/instructions across every call in a session.
  - Retrieved context goes in the user turn (changes per query) and is not cached.
"""

import anthropic
import pandas as pd
from langchain_community.document_loaders import PyPDFLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS

_SYSTEM_PROMPT = """\
You are a Senior NPI Engineer for the Aviator (SM8845) project.
Answer questions using only the retrieved context from the DOI, SOP, and BOM.
If the answer is not in the context, say so explicitly — do not fabricate parameters.
Always quote exact technical parameters (torque spec, pressure, weight, part number, \
step number) when present in the context.
For RCA questions, identify the Physics-of-Failure mechanism before recommending action.\
"""


class AviatorRAGSystem:
    def __init__(self, pdf_docs: list[str], golden_sample_csv: str) -> None:
        self.embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
        self.vector_db: FAISS | None = None
        self.golden_sample = pd.read_csv(golden_sample_csv)
        self.client = anthropic.Anthropic()
        self._initialize_docs(pdf_docs)

    def _initialize_docs(self, pdf_paths: list[str]) -> None:
        """Ingest DOI/SOP PDFs into the FAISS vector store."""
        all_docs = []
        for path in pdf_paths:
            loader = PyPDFLoader(path)
            all_docs.extend(loader.load())

        splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=150)
        chunks = splitter.split_documents(all_docs)
        self.vector_db = FAISS.from_documents(chunks, self.embeddings)

    def check_parametric_shift(self, current_logs_csv: str) -> pd.DataFrame:
        """Stage 2: flag any test value that drifted >5 % from golden sample."""
        current = pd.read_csv(current_logs_csv)
        alerts: list[dict] = []

        for _, row in current.iterrows():
            test_code = row["TESTCODE"]
            current_val = row["TESTVAL"]

            golden = self.golden_sample.loc[
                self.golden_sample["TESTCODE"] == test_code, "TESTVAL"
            ].values

            if len(golden):
                ref = golden[0]
                pct = (abs(current_val - ref) / ref) * 100 if ref != 0 else 0
                if pct > 5:
                    alerts.append(
                        {
                            "Test": test_code,
                            "Golden": ref,
                            "Current": current_val,
                            "Shift": f"{pct:.2f}%",
                            "Status": "PROCESS_DRIFT_ALERT",
                        }
                    )

        return pd.DataFrame(alerts)

    def query_engine(
        self,
        user_query: str,
        k: int = 3,
        show_thinking: bool = False,
    ) -> str:
        """
        Retrieve top-k document chunks, then ask Claude for RCA / control-plan analysis.

        Streams the response to stdout as it arrives.
        Returns the full text answer.
        """
        assert self.vector_db is not None, "Call _initialize_docs() first."

        # --- Retrieve relevant chunks ----------------------------------------
        docs = self.vector_db.similarity_search(user_query, k=k)
        context_blocks = []
        for doc in docs:
            src = doc.metadata.get("source", "DOI/SOP")
            page = doc.metadata.get("page", "?")
            context_blocks.append(f"[{src}  p.{page}]\n{doc.page_content}")
        context = "\n\n---\n\n".join(context_blocks)

        # --- Stream Claude response -------------------------------------------
        response_text = ""

        with self.client.messages.stream(
            model="claude-opus-4-7",
            max_tokens=4096,
            # Adaptive thinking: Claude decides when to reason deeply.
            # display:"summarized" makes the thinking text visible so it can
            # optionally be shown to the engineer for traceability.
            thinking={"type": "adaptive", "display": "summarized"},
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    # cache_control marks the system prompt as a stable prefix.
                    # Cache hits yield ~90 % cost reduction on those tokens.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Retrieved context from project documents:\n\n"
                        f"{context}\n\n"
                        f"---\n\n"
                        f"Question: {user_query}"
                    ),
                }
            ],
        ) as stream:
            current_block_type = ""

            for event in stream:
                if event.type == "content_block_start":
                    current_block_type = event.content_block.type
                    if current_block_type == "thinking" and show_thinking:
                        print("\n[Reasoning]\n", flush=True)
                    elif current_block_type == "text":
                        if show_thinking:
                            print("\n[Answer]\n", flush=True)

                elif event.type == "content_block_delta":
                    if event.delta.type == "thinking_delta":
                        if show_thinking:
                            print(event.delta.thinking, end="", flush=True)
                    elif event.delta.type == "text_delta":
                        print(event.delta.text, end="", flush=True)
                        response_text += event.delta.text

            final = stream.get_final_message()

        u = final.usage
        print(
            f"\n\n[Tokens  uncached={u.input_tokens}"
            f"  cache_write={u.cache_creation_input_tokens}"
            f"  cache_hit={u.cache_read_input_tokens}"
            f"  output={u.output_tokens}]"
        )
        return response_text


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    system = AviatorRAGSystem(
        pdf_docs=["Aviator_DOI_V3.pdf", "Aviator_SOP_Mainboard.pdf"],
        golden_sample_csv="Aviator_Testing_Golden.csv",
    )

    # RCA query: Claude retrieves the fixture spec and 200 N pressure requirement
    answer = system.query_engine(
        "Mic sealing failure at Step 16 — what fixture and pressure spec does the DOI require?",
        show_thinking=True,
    )

    # Parametric shift check
    # drift = system.check_parametric_shift("Production_Logs_Shift_B.csv")
    # print(drift)

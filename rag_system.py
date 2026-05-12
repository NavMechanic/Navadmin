import os
import pandas as pd
from langchain_community.document_loaders import PyPDFLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain.prompts import PromptTemplate
from langchain.chains import RetrievalQA

SYSTEM_PROMPT_TEMPLATE = """
You are a Senior NPI Engineer for the Aviator (SM8845) project.
Use the following pieces of context from the DOI, SOP, and BOM to answer the question.
If you don't know the answer, say you don't know, don't make up an answer.

Context: {context}
Question: {question}

Instruction: Provide exact technical parameters (Torque, Pressure, Weight) if found.
Answer:"""

DRIFT_THRESHOLD_PERCENT = 5.0


class AviatorRAGSystem:
    """RAG system for NPI engineering RCA and parametric shift detection."""

    def __init__(self, pdf_docs: list[str], golden_sample_csv: str, llm=None):
        self.embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
        self.vector_db = None
        self.llm = llm
        self.golden_sample = pd.read_csv(golden_sample_csv)
        self._initialize_docs(pdf_docs)

    def _initialize_docs(self, pdf_paths: list[str]) -> None:
        """Load PDFs and build the FAISS vector index."""
        all_docs = []
        for path in pdf_paths:
            if not os.path.exists(path):
                raise FileNotFoundError(f"Document not found: {path}")
            loader = PyPDFLoader(path)
            all_docs.extend(loader.load())

        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=150,
        )
        chunks = text_splitter.split_documents(all_docs)
        self.vector_db = FAISS.from_documents(chunks, self.embeddings)

    def save_index(self, path: str) -> None:
        """Persist the FAISS index to disk for reuse."""
        if self.vector_db is None:
            raise RuntimeError("Vector DB not initialized.")
        self.vector_db.save_local(path)

    def load_index(self, path: str) -> None:
        """Load a previously saved FAISS index."""
        self.vector_db = FAISS.load_local(path, self.embeddings, allow_dangerous_deserialization=True)

    def check_parametric_shift(self, current_logs_csv: str) -> pd.DataFrame:
        """Compare production log values against the golden sample.

        Returns a DataFrame of test codes that exceed the drift threshold.
        """
        current_data = pd.read_csv(current_logs_csv)
        required_cols = {"TESTCODE", "TESTVAL"}
        if not required_cols.issubset(current_data.columns):
            raise ValueError(f"CSV must contain columns: {required_cols}")

        golden_indexed = self.golden_sample.set_index("TESTCODE")["TESTVAL"]
        report = []

        for _, row in current_data.iterrows():
            test_code = row["TESTCODE"]
            current_val = row["TESTVAL"]

            if test_code not in golden_indexed.index:
                continue

            golden_val = golden_indexed[test_code]
            diff = abs(current_val - golden_val)
            percentage_shift = (diff / golden_val * 100) if golden_val != 0 else 0.0

            if percentage_shift > DRIFT_THRESHOLD_PERCENT:
                report.append(
                    {
                        "Test": test_code,
                        "Golden": golden_val,
                        "Current": current_val,
                        "Shift": f"{percentage_shift:.2f}%",
                        "Status": "PROCESS_DRIFT_ALERT",
                    }
                )

        return pd.DataFrame(report)

    def query_engine(self, user_query: str) -> str:
        """Run a RAG query against the loaded engineering documents.

        Requires an LLM to be provided at construction time.
        """
        if self.vector_db is None:
            raise RuntimeError("Vector DB not initialized.")
        if self.llm is None:
            raise RuntimeError(
                "No LLM configured. Pass an LLM instance to AviatorRAGSystem(llm=...)."
            )

        prompt = PromptTemplate(
            template=SYSTEM_PROMPT_TEMPLATE,
            input_variables=["context", "question"],
        )

        chain = RetrievalQA.from_chain_type(
            llm=self.llm,
            chain_type="stuff",
            retriever=self.vector_db.as_retriever(search_kwargs={"k": 3}),
            chain_type_kwargs={"prompt": prompt},
        )
        result = chain.invoke({"query": user_query})
        return result.get("result", "")

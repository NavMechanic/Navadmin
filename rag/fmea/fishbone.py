"""Mermaid fishbone (Ishikawa) diagram generator for FMEA failure modes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rag.fmea.fmea import FMEAItem


@dataclass
class FishboneDiagram:
    """Represents a fishbone diagram in Mermaid syntax."""
    title: str
    effect: str
    categories: dict[str, list[str]]  # category → list of causes
    mermaid_code: str = ""

    def __post_init__(self):
        self.mermaid_code = self.generate_mermaid()

    def generate_mermaid(self) -> str:
        """Generate Mermaid fishbone diagram code."""
        lines = [
            "graph LR",
            f"    Effect[{self.effect}]",
            "",
        ]

        # Map FMEA causes to standard fishbone categories if not provided
        if not self.categories or not self.categories.get("Causes"):
            return "\n".join(lines + ["    %% No causes specified"])

        # Create bone structure
        for i, cause in enumerate(self.categories.get("Causes", [])):
            # Sanitize cause text for Mermaid (remove special chars)
            safe_cause = cause.replace("[", "(").replace("]", ")").replace('"', "'")
            node_id = f"Cause{i}"
            lines.append(f'    {node_id}["{safe_cause}"]')
            lines.append(f"    {node_id} --> Effect")

        return "\n".join(lines)

    def to_html(self, height: int = 400) -> str:
        """Return HTML/iframe code for embedding in Streamlit."""
        import base64
        encoded = base64.b64encode(self.mermaid_code.encode()).decode()
        return f"""
        <iframe
            src="data:text/html;base64,PHNjcmlwdCBzcmM9J2h0dHBzOi8vY2RuLmpzZGVsaXZyLm5ldC9ucG0vbWVybWFpZEAvOS80LjAvbWVybWFpZC5taW4uanMnPjwvc2NyaXB0Pgo8ZGl2IGNsYXNzPSdtZXJtYWlkJz4KCXt7bWVybWFpZENvZGV9fQoKPC9kaXY+Cjxzc3lsZT4KCXNjcmlwdHt7IC5zcmM9J2h0dHBzOi8vY2RuLmpzZGVsaXZyLm5ldC9ucG0vbWVybWFpZEAvOS80LjAvbWVybWFpZC5taW4uanMnIH19Cjwvc3N0eWxlPg=="
            style="width:100%;height:{height}px;border:none;"
        />
        """


class FishboneGenerator:
    """Generate fishbone diagrams from FMEA items."""

    @staticmethod
    def from_fmea_item(item: FMEAItem) -> FishboneDiagram:
        """Create a fishbone diagram from an FMEA item.

        Parameters
        ----------
        item : FMEAItem
            FMEA item with failure_mode and causes.

        Returns
        -------
        FishboneDiagram
        """
        categories = {
            "Causes": item.cause_list if hasattr(item, "cause_list") else [],
        }

        return FishboneDiagram(
            title=f"RCA: {item.item_id} — {item.failure_mode}",
            effect=item.failure_mode,
            categories=categories,
        )

    @staticmethod
    def batch_diagrams(items: list[FMEAItem]) -> list[FishboneDiagram]:
        """Generate fishbone diagrams for multiple FMEA items."""
        return [FishboneGenerator.from_fmea_item(item) for item in items]

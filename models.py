"""
Data models for FMEA and Fishbone (Ishikawa) diagram analysis.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class IshikawaCategory(str, Enum):
    MATERIAL = "Material"
    METHOD = "Method"
    MACHINE = "Machine"
    MAN = "Man"
    MEASUREMENT = "Measurement"
    ENVIRONMENT = "Environment"


@dataclass
class Cause:
    description: str
    category: IshikawaCategory
    sub_causes: List[str] = field(default_factory=list)
    physics_of_failure: Optional[str] = None


@dataclass
class FMEAEntry:
    item: str
    failure_mode: str
    effect: str
    severity: int       # 1-10: impact of failure on end user/system
    cause: str
    occurrence: int     # 1-10: likelihood the cause leads to failure
    controls: str
    detection: int      # 1-10: likelihood current controls miss the failure
    recommended_action: str
    physics_of_failure: Optional[str] = None

    def __post_init__(self) -> None:
        for name, val in [("severity", self.severity),
                          ("occurrence", self.occurrence),
                          ("detection", self.detection)]:
            if not 1 <= val <= 10:
                raise ValueError(f"{name} must be between 1 and 10, got {val}")

    @property
    def rpn(self) -> int:
        return self.severity * self.occurrence * self.detection

    @property
    def risk_level(self) -> str:
        n = self.rpn
        if n >= 200:
            return "CRITICAL"
        if n >= 125:
            return "HIGH"
        if n >= 50:
            return "MEDIUM"
        return "LOW"


@dataclass
class FMEAAnalysis:
    title: str
    system: str
    problem_statement: str
    causes: List[Cause]
    fmea_entries: List[FMEAEntry]
    date: Optional[str] = None
    engineer: Optional[str] = None

"""Pipeline D: FEA mesh parser for stress voxel analysis."""

import csv
import json
import math
import os
import warnings
from dataclasses import dataclass, field
from typing import Optional

from rag.ingestion.loaders import Document


@dataclass
class VoxelStressNode:
    """Represents a peak stress voxel from FEA output."""
    node_id: str
    x: float
    y: float
    z: float
    von_mises_stress_mpa: float
    temperature_c: Optional[float]
    is_peak: bool  # top 5%
    metadata: dict = field(default_factory=dict)


class SpatialQuery:
    """Spatial bounding box query for vector database retrieval."""

    def __init__(
        self,
        x_range: Optional[tuple] = None,
        y_range: Optional[tuple] = None,
        z_range: Optional[tuple] = None,
    ) -> None:
        self.x_range = x_range
        self.y_range = y_range
        self.z_range = z_range


class FEAVoxelizer:
    """Converts FEA mesh data (JSON/CSV format) into spatial voxel groups."""

    VOXEL_SIZE_MM = 1.0
    PEAK_PERCENTILE = 95  # top 5%

    def _snap_to_voxel(self, coord: float) -> float:
        """Round coordinate to nearest voxel grid point."""
        return round(coord / self.VOXEL_SIZE_MM) * self.VOXEL_SIZE_MM

    def _mark_peak_nodes(self, nodes: list[VoxelStressNode]) -> list[VoxelStressNode]:
        """Mark the top PEAK_PERCENTILE% stress nodes as peaks."""
        if not nodes:
            return nodes

        stresses = sorted(n.von_mises_stress_mpa for n in nodes)
        idx = int(math.ceil(len(stresses) * self.PEAK_PERCENTILE / 100.0)) - 1
        idx = max(0, min(idx, len(stresses) - 1))
        threshold = stresses[idx]

        for node in nodes:
            node.is_peak = node.von_mises_stress_mpa >= threshold

        return nodes

    def parse_json_mesh(self, path: str) -> list[VoxelStressNode]:
        """Parse FEA results JSON with node_id, x, y, z, von_mises_mpa, temp_c fields.

        Clusters nodes into 1mm³ voxels and identifies top 5% peak stress nodes.

        Args:
            path: Path to JSON file.

        Returns:
            List of VoxelStressNode objects.
        """
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            warnings.warn(f"FEAVoxelizer: cannot parse {path}: {exc}")
            return []

        raw_nodes = data.get("nodes", data) if isinstance(data, dict) else data
        if not isinstance(raw_nodes, list):
            warnings.warn(f"FEAVoxelizer: unexpected JSON structure in {path}")
            return []

        simulation = data.get("simulation", "") if isinstance(data, dict) else ""
        units = data.get("units", {}) if isinstance(data, dict) else {}

        nodes: list[VoxelStressNode] = []
        for entry in raw_nodes:
            try:
                node = VoxelStressNode(
                    node_id=str(entry.get("node_id", f"N_{len(nodes):04d}")),
                    x=self._snap_to_voxel(float(entry["x"])),
                    y=self._snap_to_voxel(float(entry["y"])),
                    z=self._snap_to_voxel(float(entry["z"])),
                    von_mises_stress_mpa=float(entry.get("von_mises_mpa", entry.get("von_mises_stress_mpa", 0.0))),
                    temperature_c=float(entry["temp_c"]) if "temp_c" in entry and entry["temp_c"] is not None else None,
                    is_peak=False,
                    metadata={
                        "source": path,
                        "simulation": simulation,
                        "units": str(units),
                        "data_type": "fea_stress_node",
                    },
                )
                nodes.append(node)
            except (KeyError, ValueError, TypeError) as exc:
                warnings.warn(f"FEAVoxelizer: skipping node entry: {exc}")

        return self._mark_peak_nodes(nodes)

    def parse_csv_mesh(self, path: str) -> list[VoxelStressNode]:
        """Parse FEA results CSV with columns: node_id,x,y,z,von_mises_mpa,temp_c.

        Args:
            path: Path to CSV file.

        Returns:
            List of VoxelStressNode objects.
        """
        nodes: list[VoxelStressNode] = []

        try:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row_num, row in enumerate(reader, start=2):
                    try:
                        stress_key = next(
                            k for k in row
                            if "von_mises" in k.lower() or "stress" in k.lower()
                        )
                        temp_key = next(
                            (k for k in row if "temp" in k.lower()), None
                        )
                        node = VoxelStressNode(
                            node_id=str(row.get("node_id", f"N_{row_num:04d}")),
                            x=self._snap_to_voxel(float(row["x"])),
                            y=self._snap_to_voxel(float(row["y"])),
                            z=self._snap_to_voxel(float(row["z"])),
                            von_mises_stress_mpa=float(row[stress_key]),
                            temperature_c=float(row[temp_key]) if temp_key and row.get(temp_key) else None,
                            is_peak=False,
                            metadata={
                                "source": path,
                                "data_type": "fea_stress_node",
                            },
                        )
                        nodes.append(node)
                    except (KeyError, ValueError, StopIteration) as exc:
                        warnings.warn(f"FEAVoxelizer: skipping CSV row {row_num}: {exc}")
        except OSError as exc:
            warnings.warn(f"FEAVoxelizer: cannot open {path}: {exc}")

        return self._mark_peak_nodes(nodes)

    def extract_bounding_box(self, nodes: list[VoxelStressNode]) -> dict:
        """Return bounding box dict covering all provided nodes.

        Returns:
            Dict with x_axis_range, y_axis_range, z_axis_range lists [min, max].
        """
        if not nodes:
            return {
                "x_axis_range": [0.0, 0.0],
                "y_axis_range": [0.0, 0.0],
                "z_axis_range": [0.0, 0.0],
            }

        xs = [n.x for n in nodes]
        ys = [n.y for n in nodes]
        zs = [n.z for n in nodes]

        return {
            "x_axis_range": [min(xs), max(xs)],
            "y_axis_range": [min(ys), max(ys)],
            "z_axis_range": [min(zs), max(zs)],
        }

    def nodes_to_documents(self, nodes: list[VoxelStressNode]) -> list[Document]:
        """Convert peak stress nodes to Documents for indexing.

        Only peak stress nodes are converted to documents to reduce noise.
        The content describes the stress hotspot in engineering language.

        Args:
            nodes: List of VoxelStressNode objects (all nodes, not just peaks).

        Returns:
            List of Documents for peak stress hotspots.
        """
        peak_nodes = [n for n in nodes if n.is_peak]
        if not peak_nodes:
            peak_nodes = nodes  # If no peaks flagged, use all

        # Group into voxel clusters by proximity
        all_bbox = self.extract_bounding_box(nodes)

        documents: list[Document] = []
        for node in peak_nodes:
            temp_str = (
                f"{node.temperature_c:.1f}°C" if node.temperature_c is not None
                else "N/A"
            )

            content = (
                f"FEA STRESS HOTSPOT — Node {node.node_id}\n"
                f"Location: x={node.x:.1f}mm, y={node.y:.1f}mm, z={node.z:.1f}mm\n"
                f"Von Mises Stress: {node.von_mises_stress_mpa:.2f} MPa\n"
                f"Temperature: {temp_str}\n"
                f"Peak stress node (top 5% of field): {'Yes' if node.is_peak else 'No'}\n"
                f"Simulation bounding box: "
                f"x∈{all_bbox['x_axis_range']}, "
                f"y∈{all_bbox['y_axis_range']}, "
                f"z∈{all_bbox['z_axis_range']} (mm)\n"
                f"Engineering note: High von Mises stress region indicates potential "
                f"fatigue or yield risk at this spatial location."
            )

            metadata = {
                **node.metadata,
                "node_id": node.node_id,
                "x": node.x,
                "y": node.y,
                "z": node.z,
                "von_mises_stress_mpa": node.von_mises_stress_mpa,
                "temperature_c": node.temperature_c,
                "is_peak": node.is_peak,
                # Spatial bounding box fields for Qdrant range filtering
                "x_min": node.x - self.VOXEL_SIZE_MM / 2,
                "x_max": node.x + self.VOXEL_SIZE_MM / 2,
                "y_min": node.y - self.VOXEL_SIZE_MM / 2,
                "y_max": node.y + self.VOXEL_SIZE_MM / 2,
                "z_min": node.z - self.VOXEL_SIZE_MM / 2,
                "z_max": node.z + self.VOXEL_SIZE_MM / 2,
                "spatial_coordinates_box": {
                    "x_axis_range": [node.x - self.VOXEL_SIZE_MM / 2, node.x + self.VOXEL_SIZE_MM / 2],
                    "y_axis_range": [node.y - self.VOXEL_SIZE_MM / 2, node.y + self.VOXEL_SIZE_MM / 2],
                    "z_axis_range": [node.z - self.VOXEL_SIZE_MM / 2, node.z + self.VOXEL_SIZE_MM / 2],
                },
                "data_type": "fea_stress_node",
            }

            # Remove None values
            metadata = {k: v for k, v in metadata.items() if v is not None}

            documents.append(Document(content=content, metadata=metadata))

        return documents

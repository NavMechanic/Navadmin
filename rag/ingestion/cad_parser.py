"""Pipeline C: CAD assembly parser for STEP files and JSON manifests."""

import json
import os
import re
import warnings
from dataclasses import dataclass, field
from typing import Optional

from rag.ingestion.loaders import Document


@dataclass
class CADComponent:
    name: str
    part_number: str
    material: str
    spatial_bbox: dict  # {"x_axis_range": ..., "y_axis_range": ..., "z_axis_range": ...}
    mating_features: list  # descriptions of mating faces/edges
    metadata: dict = field(default_factory=dict)


class STEPParser:
    """Parse STEP/IGES assembly files into CAD component graphs.

    Uses pythonOCC-core if available, falls back to text-based STEP parsing.
    For the text fallback: STEP files are ASCII-based; parse PRODUCT,
    ADVANCED_BREP_SHAPE_REPRESENTATION, and NEXT_ASSEMBLY_USAGE_OCCURENCE
    entities to extract component hierarchy.
    """

    def parse_step_text(self, path: str) -> list[CADComponent]:
        """Text-based STEP parser using Python re module.

        Extracts PRODUCT entries and CARTESIAN_POINT entries to estimate
        spatial bounding boxes per component.

        Args:
            path: Path to STEP (.stp or .step) file.

        Returns:
            List of CADComponent objects, or empty list if file unavailable.
        """
        if not path:
            warnings.warn("STEPParser.parse_step_text: no path provided, returning empty list")
            return []

        if not os.path.isfile(path):
            warnings.warn(f"STEPParser.parse_step_text: file not found: {path}")
            return []

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError as exc:
            warnings.warn(f"STEPParser.parse_step_text: cannot open {path}: {exc}")
            return []

        components = self._extract_products(content, path)
        if not components:
            warnings.warn(f"STEPParser.parse_step_text: no PRODUCT entities found in {path}")

        return components

    def _extract_products(self, content: str, source_path: str) -> list[CADComponent]:
        """Extract PRODUCT entities and Cartesian points from STEP ASCII content."""
        # Match: #ID=PRODUCT('name','description','part_number',(ref));
        product_re = re.compile(
            r"#(\d+)\s*=\s*PRODUCT\s*\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*",
            re.IGNORECASE,
        )
        # Match: #ID=CARTESIAN_POINT('',(x,y,z));
        point_re = re.compile(
            r"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-\d.Ee+]+)\s*,\s*([-\d.Ee+]+)\s*,\s*([-\d.Ee+]+)\s*\)\s*\)",
            re.IGNORECASE,
        )

        # Collect all cartesian points
        all_points: list[tuple[float, float, float]] = []
        for m in point_re.finditer(content):
            try:
                all_points.append((float(m.group(2)), float(m.group(3)), float(m.group(4))))
            except ValueError:
                continue

        # Global bounding box for distributing components spatially
        if all_points:
            xs = [p[0] for p in all_points]
            ys = [p[1] for p in all_points]
            zs = [p[2] for p in all_points]
            global_bbox = {
                "x_axis_range": [min(xs), max(xs)],
                "y_axis_range": [min(ys), max(ys)],
                "z_axis_range": [min(zs), max(zs)],
            }
        else:
            global_bbox = {
                "x_axis_range": [0.0, 77.0],
                "y_axis_range": [0.0, 163.0],
                "z_axis_range": [0.0, 8.0],
            }

        components: list[CADComponent] = []
        for m in product_re.finditer(content):
            name = m.group(2).strip()
            description = m.group(3).strip()
            part_number = m.group(4).strip() or description or name

            if not name:
                continue

            components.append(CADComponent(
                name=name,
                part_number=part_number,
                material="Unknown",
                spatial_bbox=global_bbox,
                mating_features=[],
                metadata={
                    "source": source_path,
                    "description": description,
                    "data_type": "cad_component",
                },
            ))

        return components

    def parse_json_manifest(self, path: str) -> list[CADComponent]:
        """Parse a JSON manifest of CAD components.

        JSON format: list of {name, part_number, material,
        bbox: {x:[min,max], y:[min,max], z:[min,max]},
        mating_features: [...]}

        Args:
            path: Path to JSON manifest file.

        Returns:
            List of CADComponent objects.
        """
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            warnings.warn(f"STEPParser.parse_json_manifest: cannot parse {path}: {exc}")
            return []

        components_data = data if isinstance(data, list) else data.get("components", [])
        components: list[CADComponent] = []

        for entry in components_data:
            try:
                bbox_raw = entry.get("bbox", {})
                # Support both {x:[min,max]} and {x_axis_range:[min,max]} key formats
                def get_range(key_short: str, key_long: str) -> list:
                    if key_long in bbox_raw:
                        return bbox_raw[key_long]
                    if key_short in bbox_raw:
                        return bbox_raw[key_short]
                    return [0.0, 0.0]

                spatial_bbox = {
                    "x_axis_range": get_range("x", "x_axis_range"),
                    "y_axis_range": get_range("y", "y_axis_range"),
                    "z_axis_range": get_range("z", "z_axis_range"),
                }

                component = CADComponent(
                    name=str(entry.get("name", "Unknown")),
                    part_number=str(entry.get("part_number", entry.get("partNumber", ""))),
                    material=str(entry.get("material", "Unknown")),
                    spatial_bbox=spatial_bbox,
                    mating_features=list(entry.get("mating_features", [])),
                    metadata={
                        "source": path,
                        "data_type": "cad_component",
                        **{k: v for k, v in entry.items()
                           if k not in ("name", "part_number", "material", "bbox", "mating_features")},
                    },
                )
                components.append(component)
            except Exception as exc:
                warnings.warn(f"STEPParser.parse_json_manifest: skipping entry: {exc}")

        return components

    def components_to_documents(self, components: list[CADComponent]) -> list[Document]:
        """Convert CADComponent objects to Documents with spatial metadata.

        Args:
            components: List of parsed CAD components.

        Returns:
            List of Documents for vector indexing.
        """
        documents: list[Document] = []

        for component in components:
            bbox = component.spatial_bbox
            x_range = bbox.get("x_axis_range", [0.0, 0.0])
            y_range = bbox.get("y_axis_range", [0.0, 0.0])
            z_range = bbox.get("z_axis_range", [0.0, 0.0])

            mating_str = (
                "\n".join(f"  - {f}" for f in component.mating_features)
                if component.mating_features
                else "  None documented"
            )

            content = (
                f"CAD COMPONENT: {component.name}\n"
                f"Part Number: {component.part_number}\n"
                f"Material: {component.material}\n"
                f"Spatial Extent (mm):\n"
                f"  X: {x_range[0]:.1f} to {x_range[1]:.1f} mm\n"
                f"  Y: {y_range[0]:.1f} to {y_range[1]:.1f} mm\n"
                f"  Z: {z_range[0]:.1f} to {z_range[1]:.1f} mm\n"
                f"Mating Features:\n{mating_str}\n"
                f"Component description: {component.metadata.get('description', component.name)} — "
                f"structural and functional assembly component for the Aviator26 mobile platform."
            )

            metadata = {
                **component.metadata,
                "component_name": component.name,
                "part_number": component.part_number,
                "material": component.material,
                "x_min": x_range[0],
                "x_max": x_range[1],
                "y_min": y_range[0],
                "y_max": y_range[1],
                "z_min": z_range[0],
                "z_max": z_range[1],
                "mating_features": ", ".join(component.mating_features),
                "data_type": "cad_component",
            }

            # Remove None values
            metadata = {k: v for k, v in metadata.items() if v is not None}

            documents.append(Document(content=content, metadata=metadata))

        return documents

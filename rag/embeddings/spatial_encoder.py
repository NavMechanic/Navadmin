"""SpatialMLPEncoder: projects spatial bounding boxes and FEA stress vectors into embedding space.

Uses a simple fixed random projection MLP (seeded for reproducibility).
No torch/tensorflow dependency — numpy only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from rag.ingestion.physics_parser import VoxelStressNode


def _relu(x: np.ndarray) -> np.ndarray:
    """Rectified linear unit activation."""
    return np.maximum(0.0, x)


def _l2_normalize(x: np.ndarray) -> np.ndarray:
    """L2-normalize a 1-D vector. Returns zero vector if norm is zero."""
    norm = np.linalg.norm(x)
    if norm < 1e-12:
        return x
    return x / norm


# Phone-sized device normalization constants (mm)
_PHONE_X_MAX = 80.0   # ~77mm wide + margin
_PHONE_Y_MAX = 170.0  # ~163mm tall + margin
_PHONE_Z_MAX = 10.0   # ~8mm thick + margin
_STRESS_MAX_MPA = 1000.0   # Practical upper bound for consumer electronics FEA
_TEMP_MAX_C = 120.0        # Practical upper bound for mobile chip temps


class SpatialMLPEncoder:
    """Projects spatial bounding boxes and FEA stress vectors into embedding space.

    Architecture: input_dim → 256 → output_dim
    Uses a fixed random projection (seed=42) for reproducible inference without training.

    Input vector (9-dim):
        [x_min, y_min, z_min, x_max, y_max, z_max, von_mises_stress_mpa, temp_c, is_peak]
        All features normalized to approximately [-1, 1].
    """

    def __init__(self, input_dim: int = 9, output_dim: int = 384) -> None:
        self.input_dim = input_dim
        self.output_dim = output_dim

        rng = np.random.RandomState(42)
        self.W1 = rng.randn(input_dim, 256).astype(np.float32) * 0.1
        self.W2 = rng.randn(256, output_dim).astype(np.float32) * 0.1

    def _normalize_features(
        self,
        x_min: float,
        y_min: float,
        z_min: float,
        x_max: float,
        y_max: float,
        z_max: float,
        stress_mpa: float,
        temp_c: float,
        is_peak: bool,
    ) -> np.ndarray:
        """Normalize raw spatial/stress features to [-1, 1] range."""
        return np.array(
            [
                x_min / _PHONE_X_MAX * 2 - 1,
                y_min / _PHONE_Y_MAX * 2 - 1,
                z_min / _PHONE_Z_MAX * 2 - 1,
                x_max / _PHONE_X_MAX * 2 - 1,
                y_max / _PHONE_Y_MAX * 2 - 1,
                z_max / _PHONE_Z_MAX * 2 - 1,
                np.clip(stress_mpa / _STRESS_MAX_MPA, 0.0, 1.0) * 2 - 1,
                np.clip(temp_c / _TEMP_MAX_C, 0.0, 1.0) * 2 - 1,
                1.0 if is_peak else -1.0,
            ],
            dtype=np.float32,
        )

    def _forward(self, x: np.ndarray) -> np.ndarray:
        """Forward pass: W1 → relu → W2 → L2 normalize."""
        h = _relu(x @ self.W1)   # (256,)
        out = h @ self.W2         # (output_dim,)
        return _l2_normalize(out)

    def encode_bbox(
        self,
        bbox: dict,
        stress_mpa: float = 0.0,
        temp_c: float = 0.0,
        is_peak: bool = False,
    ) -> np.ndarray:
        """Encode a spatial bounding box dict into an embedding vector.

        Args:
            bbox: Dict with x_axis_range, y_axis_range, z_axis_range keys,
                  each being [min, max] float pairs.
            stress_mpa: Von Mises stress in MPa (default 0).
            temp_c: Temperature in Celsius (default 0).
            is_peak: Whether this is a peak stress voxel.

        Returns:
            L2-normalized numpy array of shape (output_dim,).
        """
        x_range = bbox.get("x_axis_range", [0.0, 0.0])
        y_range = bbox.get("y_axis_range", [0.0, 0.0])
        z_range = bbox.get("z_axis_range", [0.0, 0.0])

        features = self._normalize_features(
            x_min=float(x_range[0]),
            y_min=float(y_range[0]),
            z_min=float(z_range[0]),
            x_max=float(x_range[1]),
            y_max=float(y_range[1]),
            z_max=float(z_range[1]),
            stress_mpa=float(stress_mpa),
            temp_c=float(temp_c),
            is_peak=bool(is_peak),
        )

        return self._forward(features)

    def encode_stress_field(self, nodes: list[Any]) -> np.ndarray:
        """Average-encode all peak stress nodes, return mean embedding.

        Args:
            nodes: List of VoxelStressNode objects. Uses all nodes if none
                   are marked as peak.

        Returns:
            Mean L2-normalized embedding across all provided nodes,
            shape (output_dim,).
        """
        if not nodes:
            return _l2_normalize(np.zeros(self.output_dim, dtype=np.float32))

        peak_nodes = [n for n in nodes if getattr(n, "is_peak", False)]
        target_nodes = peak_nodes if peak_nodes else nodes

        embeddings = []
        for node in target_nodes:
            bbox = {
                "x_axis_range": [node.x, node.x],
                "y_axis_range": [node.y, node.y],
                "z_axis_range": [node.z, node.z],
            }
            emb = self.encode_bbox(
                bbox=bbox,
                stress_mpa=getattr(node, "von_mises_stress_mpa", 0.0),
                temp_c=getattr(node, "temperature_c", 0.0) or 0.0,
                is_peak=getattr(node, "is_peak", False),
            )
            embeddings.append(emb)

        mean_emb = np.mean(embeddings, axis=0).astype(np.float32)
        return _l2_normalize(mean_emb)

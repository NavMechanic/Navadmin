#!/usr/bin/env python3
"""
Test script demonstrating complete Phase Orchestration (6-phase pipeline).

Run: python test_phase_orchestration.py
"""

import sys
from datetime import datetime
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent))

from phases.phase_orchestrator import PhaseOrchestrator


def main():
    """Run complete phase orchestration test."""
    orchestrator = PhaseOrchestrator()

    # Test run parameters
    run_id = f"ORCH-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    design_id = "DESIGN-WIFI-BEACON-V2"
    market_size = 50000  # 50k units annual market
    quantity = 10000     # First manufacturing run: 10k units

    # Execute full pipeline
    run = orchestrator.execute_full_pipeline(
        run_id=run_id,
        design_id=design_id,
        market_size=market_size,
        quantity=quantity,
        use_cloud_sim=False,  # Skip cloud simulation for speed
    )

    # Print detailed results
    print("\n" + "="*70)
    print("DETAILED PHASE RESULTS")
    print("="*70 + "\n")

    for phase_name, result in run.phases.items():
        print(f"\n{result.phase_name}")
        print("-" * 70)
        print(f"  Status: {result.status.value}")
        print(f"  Duration: {result.duration_ms:.2f}ms")

        if result.error_message:
            print(f"  Error: {result.error_message}")

        if result.result_data:
            print(f"  Results:")
            for key, value in result.result_data.items():
                if isinstance(value, dict):
                    print(f"    {key}:")
                    for k, v in value.items():
                        print(f"      {k}: {v}")
                else:
                    print(f"    {key}: {value}")

    # Summary statistics
    print("\n" + "="*70)
    print("ORCHESTRATION SUMMARY")
    print("="*70 + "\n")
    print(f"Run ID:              {run.run_id}")
    print(f"Design:              {run.design_id}")
    print(f"Status:              {run.status}")
    print(f"Created:             {run.created_at.isoformat()}")
    print(f"Completed:           {run.completed_at.isoformat()}")
    print(f"Total Duration:      {run.total_duration_ms/1000:.2f}s")
    print(f"Phases Completed:    {sum(1 for p in run.phases.values() if p.status.value == 'success')}/6")

    # Export run data
    print("\n" + "="*70)
    print("EXPORTING RUN DATA")
    print("="*70 + "\n")

    run_json = run.to_dict()
    print(f"Run data (JSON):\n{run_json}")

    print(f"\n✅ Phase Orchestration test complete!")


if __name__ == "__main__":
    main()

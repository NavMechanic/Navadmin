"""
Pre-built failure-mode templates for common naval electronics defects.
Each template encodes Physics-of-Failure (PoF) mechanics so engineers
get actionable root-cause language rather than generic advice.
"""

from models import Cause, FMEAAnalysis, FMEAEntry, IshikawaCategory


# ---------------------------------------------------------------------------
# Template 1 – Solder Joint Reliability (BGA / QFP / Through-hole)
# ---------------------------------------------------------------------------

SOLDER_JOINT_FAILURE = FMEAAnalysis(
    title="Solder Joint Reliability",
    system="PCB Assembly — Surface Mount Technology",
    problem_statement="Solder Joint Failure",
    causes=[
        Cause(
            description="Wrong solder alloy",
            category=IshikawaCategory.MATERIAL,
            sub_causes=[
                "SAC305 substituted with Sn63Pb37 in lead-free design",
                "Counterfeit solder paste lot",
            ],
            physics_of_failure=(
                "Mixing SnAgCu and SnPb alloys shifts the liquidus above the "
                "design reflow peak, leaving partially solidified joints. "
                "Bi-metallic IMC layers (Cu3Sn / Cu6Sn5) grow at different "
                "rates, causing Kirkendall voiding under thermal cycling."
            ),
        ),
        Cause(
            description="Expired or mis-stored paste",
            category=IshikawaCategory.MATERIAL,
            sub_causes=[
                "Flux activator decomposed beyond shelf life",
                "Cold-chain break — paste stored above 10 °C",
            ],
            physics_of_failure=(
                "Organic acid flux activators degrade via esterification, "
                "reducing oxide removal capacity. Insufficient oxide reduction "
                "creates dewetting, head-in-pillow, and cold-solder joints."
            ),
        ),
        Cause(
            description="Incorrect reflow profile",
            category=IshikawaCategory.METHOD,
            sub_causes=[
                "Peak temperature below alloy liquidus",
                "Time-above-liquidus (TAL) < 45 s",
                "Ramp rate > 3 °C/s causing slump",
            ],
            physics_of_failure=(
                "TAL < 45 s prevents full flux activation and IMC nucleation. "
                "Rapid cool-down (> 4 °C/s) induces dendritic Ag3Sn growth "
                "perpendicular to the pad, acting as fatigue crack nucleation sites."
            ),
        ),
        Cause(
            description="Stencil aperture mis-match",
            category=IshikawaCategory.METHOD,
            sub_causes=[
                "Area ratio < 0.66 causing insufficient paste transfer",
                "Worn stencil aperture walls",
            ],
            physics_of_failure=(
                "Area ratio < 0.66 traps paste in the aperture; the resulting "
                "insufficient volume leaves thin heel fillets on QFPs, the "
                "highest-stress zone under board bending (pad cratering risk)."
            ),
        ),
        Cause(
            description="Oven temperature non-uniformity",
            category=IshikawaCategory.MACHINE,
            sub_causes=[
                "Conveyor speed drift",
                "Heating element degradation in zone 4",
            ],
            physics_of_failure=(
                "ΔT > 5 °C across board creates differential IMC growth: "
                "hotter zones form thicker, more brittle Cu6Sn5 layers faster, "
                "concentrating thermal-fatigue damage at those joints first."
            ),
        ),
        Cause(
            description="Pick-and-place placement offset",
            category=IshikawaCategory.MACHINE,
            sub_causes=[
                "Vision system calibration drift",
                "Nozzle worn — component skew",
            ],
            physics_of_failure=(
                "Lateral offset > 25 % of pad width introduces asymmetric "
                "wetting forces during reflow, rotating the component (tombstoning "
                "risk for 0402) and producing asymmetric fillet geometry that "
                "concentrates cyclic strain at one heel."
            ),
        ),
        Cause(
            description="ESD / mechanical damage during handling",
            category=IshikawaCategory.MAN,
            sub_causes=[
                "Board handled without wrist strap",
                "Connector insertion force cracking nearby joints",
            ],
            physics_of_failure=(
                "Board bending during connector mating applies Mode-I fracture "
                "loading to solder joints. BGA corner balls see the highest "
                "peel stress; pad cratering initiates in FR-4 resin below "
                "the copper pad, not in the solder itself."
            ),
        ),
        Cause(
            description="Inadequate AOI programming",
            category=IshikawaCategory.MEASUREMENT,
            sub_causes=[
                "Fillet height thresholds too permissive",
                "Missing X-ray inspection for BGA",
            ],
            physics_of_failure=(
                "AOI cameras cannot resolve sub-surface voids. IPC-7095 "
                "classifies BGA voids > 25 % ball cross-section as defects; "
                "undetected voids reduce thermal cycling life per the "
                "Coffin-Manson relationship (Nf ∝ ΔT^−2.65)."
            ),
        ),
        Cause(
            description="High relative humidity in production area",
            category=IshikawaCategory.ENVIRONMENT,
            sub_causes=[
                "RH > 70 % — paste absorbs moisture",
                "PCB moisture absorption — popcorn cracking at reflow",
            ],
            physics_of_failure=(
                "Moisture absorbed into flux lowers its boiling point, causing "
                "steam generation mid-reflow (solder balling). In high-Tg laminates, "
                "absorbed moisture vaporizes explosively above 180 °C — popcorn "
                "cracking delaminating pad from inner layers."
            ),
        ),
        Cause(
            description="Ship-board vibration during operations",
            category=IshikawaCategory.ENVIRONMENT,
            sub_causes=[
                "Resonant frequency of PCB not damped",
                "Conformal coating absent or insufficient",
            ],
            physics_of_failure=(
                "Sinusoidal vibration at the PCB first resonant frequency drives "
                "high-cycle fatigue in solder joints per MIL-STD-810H Method 514. "
                "Corner BGA balls and leaded device heel fillets accumulate damage "
                "fastest; Miner's rule summation determines cumulative fatigue fraction."
            ),
        ),
    ],
    fmea_entries=[
        FMEAEntry(
            item="BGA Solder Ball",
            failure_mode="Open circuit — thermal fatigue crack",
            effect="Component non-functional; mission-critical system failure",
            severity=9,
            cause="Thermal cycling exceeds solder fatigue life (Coffin-Manson)",
            occurrence=5,
            controls="X-ray inspection post-reflow; in-circuit test (ICT)",
            detection=4,
            recommended_action=(
                "Reduce ΔT by adding thermal relief; add underfill on corner "
                "balls; verify TAL ≥ 60 s; requalify profile per IPC J-STD-020."
            ),
            physics_of_failure=(
                "Nf ∝ (ΔεT / 2)^−c per Coffin-Manson. Each 10 °C reduction in "
                "ΔT roughly doubles fatigue life. Underfill reduces solder strain "
                "energy density by transferring load to the epoxy."
            ),
        ),
        FMEAEntry(
            item="QFP Lead Fillet",
            failure_mode="Heel crack — open circuit under vibration",
            effect="Intermittent signal loss; built-in-test false alarms",
            severity=7,
            cause="High-cycle fatigue from ship-board vibration (MIL-STD-810H)",
            occurrence=4,
            controls="Visual inspection per IPC-A-610; functional test",
            detection=5,
            recommended_action=(
                "Apply conformal coat (IPC-CC-830B class AR/UR); add staking "
                "compound at lead tips; review PCB support/damping scheme."
            ),
            physics_of_failure=(
                "Heel fillet is the minimum cross-section; von Mises stress peaks "
                "here under cantilever loading. Miner's Rule: D = Σ(ni/Ni) ≥ 1 "
                "triggers crack initiation. Conformal coat adds damping, raising Ni."
            ),
        ),
        FMEAEntry(
            item="PCB Pad (BGA corner)",
            failure_mode="Pad cratering — laminate fracture",
            effect="Irreparable board damage; scrapped unit",
            severity=10,
            cause="Excessive connector insertion force or PCB flex during assembly",
            occurrence=3,
            controls="Assembly fixture to limit board deflection; work instruction",
            detection=6,
            recommended_action=(
                "Implement board support fixture at connector stations; "
                "add IPC/JEDEC JESD22-B111 bend test to qualification; "
                "specify higher Tg laminate (FR408HR or Rogers 4350)."
            ),
            physics_of_failure=(
                "Pad cratering initiates in the resin-rich zone of FR-4 "
                "below the Cu pad when peel stress exceeds resin fracture toughness "
                "(KIc ≈ 0.5 MPa√m for standard FR-4). High-Tg laminates raise "
                "KIc and shift fracture to the solder, which is reparable."
            ),
        ),
        FMEAEntry(
            item="Through-hole Solder Joint",
            failure_mode="Cold solder — resistive joint",
            effect="Elevated contact resistance; signal degradation",
            severity=6,
            cause="Insufficient flux activation — RH > 70 % or expired paste",
            occurrence=4,
            controls="SPC on paste date codes; paste rheology test (slump/tack)",
            detection=5,
            recommended_action=(
                "Enforce paste shelf-life tracking in MRP; install RH control "
                "(40–60 %); add in-line paste inspection (SPI) with volume alarm."
            ),
            physics_of_failure=(
                "Organic acid flux half-life drops exponentially above 10 °C "
                "storage (Arrhenius). Degraded flux fails to reduce Cu-oxide (ΔG > 0), "
                "leaving a Cu2O layer that manifests as a resistive, grainy solder joint."
            ),
        ),
        FMEAEntry(
            item="Fine-pitch SMD (0402)",
            failure_mode="Tombstoning — one pad open",
            effect="Component absence on one pad; open circuit",
            severity=8,
            cause="Asymmetric paste volume between pads causes imbalanced surface tension",
            occurrence=4,
            controls="SPI volume check per pad; AOI coplanarity",
            detection=3,
            recommended_action=(
                "Adjust stencil aperture to equalize paste volume; "
                "verify component placement within ±50 µm; "
                "tune zone 3 ramp rate to ≤ 1.5 °C/s to allow simultaneous wetting."
            ),
            physics_of_failure=(
                "Tombstoning is driven by unequal surface-tension torque at the "
                "two pads during simultaneous wetting. Torque imbalance = "
                "γ·(A1 − A2)·cos θ; reducing paste-volume asymmetry "
                "to < 10 % eliminates the driving torque."
            ),
        ),
        FMEAEntry(
            item="Solder Joint (any)",
            failure_mode="Tin whisker shorting adjacent conductors",
            effect="Latent short circuit; potential arcing in high-voltage section",
            severity=8,
            cause="Pure Sn or high-Sn finish on component terminations under compressive stress",
            occurrence=3,
            controls="Visual inspection at 40× (IPC-A-610); conformal coat",
            detection=7,
            recommended_action=(
                "Require SAC305 or SnCuNi finish on all component terminations; "
                "specify IPC/JEDEC JESD201A whisker testing; "
                "apply conformal coat ≥ 50 µm to bridge potential whisker path."
            ),
            physics_of_failure=(
                "Tin whiskers grow from compressive stress in the Sn layer driven "
                "by Cu6Sn5 IMC formation beneath the finish. Growth rate ∝ "
                "compressive stress × diffusivity. Ag or Bi dopants (SAC305) "
                "relax stress by solid-solution hardening, suppressing nucleation."
            ),
        ),
        FMEAEntry(
            item="PCB Laminate",
            failure_mode="Conductive anodic filament (CAF) — insulation resistance drop",
            effect="Leakage current; logic error or latch-up in adjacent nets",
            severity=7,
            cause="High humidity + voltage bias across drill-hole spacing < 250 µm",
            occurrence=3,
            controls="IR test at 100 V, 85 °C/85 % RH (HAST)",
            detection=5,
            recommended_action=(
                "Specify CAF-resistant laminate (IPC-4101 slash sheet /126); "
                "increase drill-to-drill clearance; enforce conformal coat; "
                "add moisture-sensitivity-level controls per IPC/JEDEC J-STD-033."
            ),
            physics_of_failure=(
                "CAF propagates along the glass-fibre/resin interface via an "
                "electrochemical dissolution–migration–deposition cycle: "
                "Cu anode → Cu²⁺ ion migration → Cu cathode deposit. "
                "Activation energy Ea ≈ 1.0 eV; rate doubles every ~25 °C."
            ),
        ),
    ],
)


# ---------------------------------------------------------------------------
# Template 2 – Connector Contact Failure
# ---------------------------------------------------------------------------

CONNECTOR_CONTACT_FAILURE = FMEAAnalysis(
    title="Electrical Connector Contact Reliability",
    system="MIL-DTL-38999 Circular Connector",
    problem_statement="Connector Contact Failure",
    causes=[
        Cause(
            description="Fretting corrosion on mating surfaces",
            category=IshikawaCategory.MATERIAL,
            sub_causes=[
                "Gold plating < 0.76 µm (MIL-G-45204 Grade B minimum)",
                "Base metal diffusion through thin plating",
            ],
            physics_of_failure=(
                "Micro-slip amplitudes of 1–10 µm at the contact interface "
                "disrupt the passivating oxide, generating abrasive SnO2 or "
                "Au-Cu oxide debris. Contact resistance rises exponentially "
                "with debris accumulation (Holm contact model)."
            ),
        ),
        Cause(
            description="Incorrect crimp tool calibration",
            category=IshikawaCategory.METHOD,
            sub_causes=[
                "Crimp height out of M22520 spec",
                "Wrong die set for wire gauge",
            ],
            physics_of_failure=(
                "Under-crimp leaves air voids; resistance rises via Holm: "
                "R = ρ/(2a) where contact radius a is reduced. "
                "Over-crimp work-hardens the conductor, embrittling it and "
                "raising susceptibility to vibration fatigue at the crimp barrel end."
            ),
        ),
        Cause(
            description="Vibration-induced fretting",
            category=IshikawaCategory.ENVIRONMENT,
            sub_causes=[
                "Connector unmounted — free to vibrate",
                "Resonant frequency of cable harness not damped",
            ],
            physics_of_failure=(
                "Ship-board vibration (MIL-STD-167-1) excites cable resonance, "
                "inducing micro-slip at the mating interface. Fretting wear "
                "rate W = k·F·d (Archard), where d = slip amplitude and "
                "k = wear coefficient. Lubricant (LPS-3) reduces k by 100×."
            ),
        ),
        Cause(
            description="Salt-fog corrosion",
            category=IshikawaCategory.ENVIRONMENT,
            sub_causes=[
                "Seal missing or damaged",
                "Backshell not fully seated",
            ],
            physics_of_failure=(
                "NaCl electrolyte forms a galvanic cell between Au contacts "
                "and Cu body. Pitting corrosion at Cu follows: "
                "i_corr = (E_cathode − E_anode) / R_total. "
                "MIL-STD-810H Method 509 requires 48 h at 5 % NaCl to qualify."
            ),
        ),
        Cause(
            description="Torque specification not followed",
            category=IshikawaCategory.MAN,
            sub_causes=[
                "Coupling nut under-torqued — ingress path open",
                "Over-torque cracking insert",
            ],
            physics_of_failure=(
                "Under-torqued coupling nut creates a capillary gap; "
                "surface tension draws condensate into the connector. "
                "Condensate combined with contaminants initiates "
                "electrolytic corrosion at the contact interface."
            ),
        ),
        Cause(
            description="Inadequate insertion/withdrawal force test",
            category=IshikawaCategory.MEASUREMENT,
            sub_causes=[
                "Go/no-go gauge not used",
                "Contact retention force not measured post-crimp",
            ],
            physics_of_failure=(
                "Retention force < MIL-DTL-39029 minimum (typically 4.4 N) "
                "allows contact back-out under vibration. The axial gap "
                "that forms increases contact resistance via the constriction "
                "resistance term in the Holm model."
            ),
        ),
    ],
    fmea_entries=[
        FMEAEntry(
            item="Socket Contact",
            failure_mode="High contact resistance — intermittent connection",
            effect="Signal dropout; built-in-test false positive",
            severity=7,
            cause="Fretting corrosion from vibration-induced micro-slip",
            occurrence=5,
            controls="Contact resistance measurement at acceptance; MIL-STD-810H vibe test",
            detection=5,
            recommended_action=(
                "Apply MIL-PRF-81309 contact lubricant; add cable clamp "
                "within 25 mm of backshell; specify 0.76 µm hard Au plating."
            ),
            physics_of_failure=(
                "Holm constriction resistance R = ρ/(2a). Fretting debris "
                "reduces effective contact radius a, raising R nonlinearly. "
                "Contact lubricant acts as a debris trap and reduces oxide "
                "formation by excluding oxygen."
            ),
        ),
        FMEAEntry(
            item="Crimp Termination",
            failure_mode="Open circuit — conductor pull-out",
            effect="Complete loss of circuit; system inoperative",
            severity=9,
            cause="Under-crimp — crimp height exceeds M22520 maximum",
            occurrence=3,
            controls="Crimp tool calibration records; pull-force test per M22520",
            detection=3,
            recommended_action=(
                "Calibrate crimp tools per M22520 schedule; add 100 % "
                "pull-test at 111 N for 20 AWG per MIL-DTL-39029; "
                "record crimp height in traveler."
            ),
            physics_of_failure=(
                "Crimp joint strength is governed by cold-welding of wire "
                "strands to barrel. Under-crimp leaves strand-to-strand air "
                "voids; tensile load is carried by fewer strands, each "
                "exceeding their fracture stress."
            ),
        ),
        FMEAEntry(
            item="Connector Shell / Seal",
            failure_mode="Moisture ingress — insulation resistance degradation",
            effect="Leakage current; corrosion of contacts; signal corruption",
            severity=8,
            cause="Damaged O-ring or coupling nut under-torque",
            occurrence=4,
            controls="Visual seal inspection; IR test at 500 V; torque verification",
            detection=4,
            recommended_action=(
                "Add torque-stripe witness mark; replace O-rings per 24-month "
                "PM cycle; perform 48 h salt-fog test per MIL-STD-810H-509 "
                "at qualification."
            ),
            physics_of_failure=(
                "EPDM O-ring compression relaxes over time (stress-relaxation "
                "Ea ≈ 0.8 eV) reducing sealing force. Once the gap opens, "
                "capillary pressure P = 4γ cos θ / d draws moisture in; "
                "at 85 % RH, liquid water films form on contact surfaces."
            ),
        ),
    ],
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TEMPLATES = {
    "solder_joint": SOLDER_JOINT_FAILURE,
    "connector_contact": CONNECTOR_CONTACT_FAILURE,
}

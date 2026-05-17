# Aviator RAG Data

This directory contains sample documents from the Aviator project extracted from Google Drive.

## Directory Structure

```
data/aviator/
├── root/                 # Component datasheets (root folder)
│   ├── battery_SA71_spec.txt
│   ├── receiver_speaker_spec.txt
│   ├── frame_front_housing_spec.txt
│   └── ...
├── smt/                  # SMT component datasheets (aviator smt folder)
│   ├── MXD8539HA_RF_switch.txt
│   └── ...
├── tester_log/           # Production test logs (tester log folder)
│   └── build_summary.txt
└── drawing/              # Engineering drawings & SOP (optional - extract from ZIP)
    └── [DOI & SOP files go here]
```

## Adding DOI & SOP Files

The **aviator drawing** folder on Google Drive contains `DOI & SOP.zip` (637MB).

### Option 1: Extract and add locally

1. Download `DOI & SOP.zip` from the [aviator drawing folder](https://drive.google.com/drive/folders/16QUsfsiZc2MhCyFMrdyHYLA_huW8bX61)
2. Extract the ZIP to `data/aviator/drawing/`:
   ```bash
   mkdir -p data/aviator/drawing
   unzip -d data/aviator/drawing /path/to/DOI_SOP.zip
   ```
3. Commit the extracted files:
   ```bash
   git add data/aviator/drawing/
   git commit -m "docs: add extracted DOI & SOP drawings from aviator Drive folder"
   git push
   ```

### Option 2: Use the RAG loader with live Drive access

```bash
python main.py ingest-drive --local-mirror data/aviator
```

The loader will automatically ingest all text-readable files (PDFs, DOCX, XLSX, TXT).

## Using the Data with the RAG Pipeline

### Ingest local data

```bash
python main.py ingest-drive --local-mirror data/aviator
```

### Query the indexed documents

```bash
python main.py query "What is the battery capacity of SA71?"
python main.py query "Which components are used in the front housing assembly?"
python main.py query "What are the L2AR test failures in the build summary?"
```

### Reset the index

```bash
python main.py reset
```

## File Descriptions

| File | Source | Content |
|------|--------|---------|
| `battery_SA71_spec.txt` | battery.docx | Motorola SA71 7100mAh Li-ion battery spec (electrical, mechanical, safety requirements) |
| `receiver_speaker_spec.txt` | receiver.pdf | XHSR121009LY-07-RH micro speaker datasheet (frequency response, impedance, reliability tests) |
| `frame_front_housing_spec.txt` | SS58F14684 drawing | Celestiq front housing assembly (antenna feeds, BOM, flatness, black coating, gaskets) |
| `MXD8539_RF_switch.txt` | SMT datasheet | Maxscend 3P3T MIPI antenna switch (0.4-5.8 GHz, insertion loss, isolation specs) |
| `build_summary.txt` | aviator build summary.xlsx | AVIATOR26 India production test data (12-16 May 2026): yield, failures, root causes |

## Google Drive Folder IDs

For reference, the source folders on Google Drive:

- **Aviator root**: `1YU-Jtitkx1IXWO1J_K_t1EBBtjb2JSE8` (46 PDFs + 1 DOCX)
- **Aviator SMT**: `19sRC9Kf2w9Zny-Gwh__I69Jpyisw0o7P` (23 component datasheets)
- **Aviator drawing**: `16QUsfsiZc2MhCyFMrdyHYLA_huW8bX61` (DOI & SOP.zip 637MB)
- **Tester log**: `1xEGu4yJjDNrJ4cuyR-1ImJeCENFp3KTW` (.log files + build summary)

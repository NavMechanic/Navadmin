#!/bin/bash
# Navadmin Quick Start Script

set -e

echo "================================"
echo "Navadmin RAG Pipeline"
echo "Quick Start Setup"
echo "================================"
echo ""

# Check Python version
python_version=$(python --version 2>&1 | awk '{print $2}')
echo "✓ Python ${python_version}"

# Install dependencies
echo "Installing dependencies..."
pip install -q -r requirements.txt
echo "✓ Dependencies installed"

# Verify imports
echo "Verifying project build..."
python -c "
from rag.pipeline import RAGPipeline
from rag.digital_twin.twin import DigitalTwin
from rag.ingestion.pdf_pipeline import PDFPipeline
print('✓ Project builds successfully')
"

echo ""
echo "================================"
echo "Setup Complete!"
echo "================================"
echo ""
echo "Next steps:"
echo ""
echo "  1. Run a batch simulation:"
echo "     python main.py twin-simulate -n 50"
echo ""
echo "  2. Launch the interactive dashboard:"
echo "     streamlit run dashboard/app.py"
echo ""
echo "  3. Show all CLI commands:"
echo "     python main.py --help"
echo ""
echo "  4. For more details, see:"
echo "     - SETUP.md (comprehensive setup guide)"
echo "     - main.py (CLI documentation)"
echo "     - dashboard/app.py (Streamlit dashboard)"
echo ""

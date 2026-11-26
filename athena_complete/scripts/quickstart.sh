#!/bin/bash
# Project Athena — Quick Start Script

set -e

echo "🚀 Project Athena — Platform Startup"
echo "======================================"

# Check prerequisites
echo "📋 Checking prerequisites..."
command -v docker >/dev/null 2>&1 || { echo "❌ Docker not found"; exit 1; }
command -v docker-compose >/dev/null 2>&1 || { echo "❌ Docker Compose not found"; exit 1; }

# Navigate to services
cd "$(dirname "$0")/../services"

# Create .env if not exists
if [ ! -f .env ]; then
    echo "📝 Creating .env from template..."
    cp .env.example .env
fi

# Build and start
echo "🔨 Building services..."
docker compose build

echo "🚀 Starting services..."
docker compose up -d

# Wait for services
echo "⏳ Waiting for services to be healthy..."
max_attempts=30
attempt=0
while [ $attempt -lt $max_attempts ]; do
    if curl -s http://localhost:8000/health > /dev/null 2>&1; then
        echo "✅ Gateway is healthy!"
        break
    fi
    attempt=$((attempt + 1))
    echo "  Attempt $attempt/$max_attempts..."
    sleep 2
done

if [ $attempt -eq $max_attempts ]; then
    echo "❌ Services failed to start"
    docker compose logs
    exit 1
fi

# Display status
echo ""
echo "✅ Platform started successfully!"
echo ""
echo "📊 Service Status:"
docker compose ps
echo ""
echo "🔑 Demo Credentials:"
echo "  Username: p.nair"
echo "  Password: p.nair-demo"
echo ""
echo "📖 API Documentation:"
echo "  • Gateway: http://localhost:8000/docs"
echo "  • Node 1:  http://localhost:8001/docs"
echo "  • Node 2:  http://localhost:8002/docs"
echo "  • Node 3:  http://localhost:8003/docs"
echo "  • Node 4:  http://localhost:8004/docs"
echo "  • Node 5:  http://localhost:8005/docs"
echo "  • Node 6:  http://localhost:8006/docs"
echo ""
echo "📚 Documentation:"
echo "  • STARTUP_GUIDE.md"
echo "  • INTEGRATION_GUIDE.md"
echo "  • IMPLEMENTATION_SUMMARY.md"
echo ""
echo "🧪 Test the platform:"
echo "  ./test-api.sh"
echo ""

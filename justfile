default:
    @just --list

# Re-vendor upstream devhub skillcards (only true external dep)
vendor:
    cp ../devhub/routes/skillcards.py oh_my_skill/routes/skillcards.py
    cp ../devhub/templates/skillcards.html oh_my_skill/templates/skillcards.html
    # Re-rewrite the absolute import after vendoring
    sed -i 's|^from shared\.config|from oh_my_skill.shared.config|' oh_my_skill/routes/skillcards.py

# Pull @dennisl0731/oh-my-ui (omi.css/nav.js/icons.svg) into static/.
# Build-time only — no npm at runtime; assets ship in the wheel for pipx.
# Usage: `just vendor-ui` (latest) or `just vendor-ui 0.2.3`
vendor-ui version="":
    scripts/vendor-ui.sh {{version}}

# Build the Docker image (standalone compose in this dir)
build:
    docker compose build

# Build the image and start the service
build-up:
    docker compose up -d --build

# Build the image and force-recreate the service
rebuild-hard:
    docker compose build
    docker compose up -d --force-recreate

up:
    docker compose up -d

down:
    docker compose stop

logs:
    docker compose logs -f

# Run tests against the package as-is (no Docker)
test:
    python -m pytest -v

test-watch:
    python -m pytest -q --tb=short -x

# Local dev server (no Docker); loads .env if present
dev:
    { [ -f .env ] && set -a && . ./.env && set +a || true; } && python -m oh_my_skill --port 5009 --no-browser

# Build a wheel for pipx smoke-testing
wheel:
    python -m pip install --quiet --upgrade hatchling build
    python -m build --wheel
    @ls -la dist/

# Install locally via pipx for smoke testing
pipx-local:
    pipx uninstall oh-my-skill || true
    pipx install .

clean:
    rm -rf build/ dist/ *.egg-info/ .pytest_cache/ __pycache__/ */__pycache__/ */*/__pycache__/

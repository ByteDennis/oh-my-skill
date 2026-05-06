default:
    @just --list

# Re-vendor upstream devhub skillcards (only true external dep)
vendor:
    cp ../devhub/routes/skillcards.py oh_my_skill/routes/skillcards.py
    cp ../devhub/templates/skillcards.html oh_my_skill/templates/skillcards.html
    # Re-rewrite the absolute import after vendoring
    sed -i 's|^from shared\.config|from oh_my_skill.shared.config|' oh_my_skill/routes/skillcards.py

# Build the Docker image (uses pyproject.toml + oh_my_skill/ package)
build:
    cd .. && docker compose build oh-my-skill

up:
    cd .. && docker compose up -d oh-my-skill

down:
    cd .. && docker compose stop oh-my-skill

logs:
    cd .. && docker compose logs -f oh-my-skill

# Run tests against the package as-is (no Docker)
test:
    python -m pytest -v

test-watch:
    python -m pytest -q --tb=short -x

# Local dev server (no Docker)
dev:
    python -m oh_my_skill --port 5009 --no-browser

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

#!/bin/bash
# Database Modernizer - Development Environment Setup Script
# This script sets up the complete development environment for new contributors

set -e  # Exit on error

# Clear any stale VIRTUAL_ENV from parent shell to avoid uv path mismatch warnings
unset VIRTUAL_ENV

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
print_header() {
    echo -e "\n${BLUE}===================================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}===================================================${NC}\n"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

# Check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Initialize rbenv if available (needed to pick up non-system Ruby)
if command_exists rbenv; then
    eval "$(rbenv init -)"
fi

# Main setup
print_header "Database Modernizer - Development Setup"

echo "This script will set up your development environment."
echo "It will:"
echo "  1. Check for required tools (Python 3.12+, Ruby 2.7+, uv, AWS CLI)"
echo "  2. Install Python dependencies with uv sync"
echo "  3. Install cfn-nag (CloudFormation security scanner)"
echo "  4. Activate virtual environment"
echo "  5. Set up git commit template"
echo "  6. Install pre-commit hooks"
echo "  7. Run initial validation"
echo ""
read -p "Continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    print_error "Setup cancelled"
    exit 1
fi

# Step 1: Check Python
print_header "Step 1: Checking Python Installation"

if command_exists python3; then
    PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
    print_success "Python found: $PYTHON_VERSION"

    # Check Python version (need 3.12+)
    PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f1)
    PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f2)

    if [ "$PYTHON_MAJOR" -ge 3 ] && [ "$PYTHON_MINOR" -ge 12 ]; then
        print_success "Python version is 3.12 or higher"
    else
        print_error "Python 3.12+ required, found $PYTHON_VERSION"
        print_info "Please install Python 3.12 or higher"
        exit 1
    fi
else
    print_error "Python 3 not found"
    print_info "Please install Python 3.11+ from https://www.python.org/"
    exit 1
fi

# Step 2: Check/Install uv
print_header "Step 2: Checking uv Installation"

if command_exists uv; then
    UV_VERSION=$(uv --version | cut -d' ' -f2)
    print_success "uv found: $UV_VERSION"
else
    print_warning "uv not found. Installing uv..."

    # Install uv
    if command_exists curl; then
        curl -LsSf https://astral.sh/uv/install.sh | sh

        # Add to PATH for current session
        export PATH="$HOME/.cargo/bin:$PATH"

        if command_exists uv; then
            print_success "uv installed successfully"
        else
            print_error "uv installation failed"
            print_info "Please install manually: https://docs.astral.sh/uv/getting-started/installation/"
            exit 1
        fi
    else
        print_error "curl not found, cannot install uv"
        print_info "Please install uv manually: https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    fi
fi

# Step 3: Check AWS CLI
print_header "Step 3: Checking AWS CLI Installation"

if command_exists aws; then
    AWS_VERSION=$(aws --version | cut -d' ' -f1 | cut -d'/' -f2)
    print_success "AWS CLI found: $AWS_VERSION"

    # Check if AWS credentials are configured
    if aws sts get-caller-identity >/dev/null 2>&1; then
        print_success "AWS credentials are configured"
    else
        print_warning "AWS credentials not configured"
        print_info "Run 'aws configure' to set up credentials"
    fi
else
    print_warning "AWS CLI not found"
    print_info "AWS CLI is required for RDS access"
    print_info "Install from: https://aws.amazon.com/cli/"
fi

# Step 4: Check Ruby and gem
print_header "Step 4: Checking Ruby Installation"

if command_exists ruby; then
    RUBY_VERSION=$(ruby --version | cut -d' ' -f2)
    print_success "Ruby found: $RUBY_VERSION"

    # Check Ruby version (need 2.7+)
    RUBY_MAJOR=$(echo "$RUBY_VERSION" | cut -d'.' -f1)
    RUBY_MINOR=$(echo "$RUBY_VERSION" | cut -d'.' -f2)

    if [ "$RUBY_MAJOR" -ge 3 ] || ([ "$RUBY_MAJOR" -eq 2 ] && [ "$RUBY_MINOR" -ge 7 ]); then
        # Check for Ruby 4.0+ which is NOT compatible with cfn-nag
        if [ "$RUBY_MAJOR" -ge 4 ]; then
            print_error "Ruby 4.0+ is NOT compatible with cfn-nag"
            print_info "cfn-nag requires Ruby 2.7 - 3.x (Ruby 3.3 recommended)"
            print_info ""
            print_info "To install Ruby 3.3 on macOS:"
            print_info "  1. Install Ruby 3.3:"
            print_info "     brew install ruby@3.3"
            print_info "  2. Add to your PATH (add to ~/.zshrc or ~/.bash_profile):"
            print_info "     export PATH=\"/opt/homebrew/opt/ruby@3.3/bin:\$PATH\""
            print_info "  3. Reload your shell:"
            print_info "     source ~/.zshrc"
            print_info "  4. Verify Ruby version:"
            print_info "     ruby --version  # Should show 3.3.x"
            print_info ""
            print_info "Or use rbenv to manage Ruby versions:"
            print_info "  brew install rbenv"
            print_info "  rbenv install 3.3.6"
            print_info "  rbenv global 3.3.6"
            exit 1
        fi

        print_success "Ruby version is compatible with cfn-nag (2.7 - 3.x)"
    else
        print_error "Ruby 2.7+ required for cfn-nag, found $RUBY_VERSION"
        print_info "Please install Ruby 3.3 (recommended):"
        print_info ""
        print_info "macOS users:"
        print_info "  1. Install Ruby 3.3 via Homebrew:"
        print_info "     brew install ruby@3.3"
        print_info "  2. Add to your PATH (add to ~/.zshrc or ~/.bash_profile):"
        print_info "     export PATH=\"/opt/homebrew/opt/ruby@3.3/bin:\$PATH\""
        print_info "  3. Reload your shell:"
        print_info "     source ~/.zshrc  # or source ~/.bash_profile"
        print_info "  4. Verify Ruby version:"
        print_info "     ruby --version"
        print_info ""
        print_info "Linux users:"
        print_info "  - Use your package manager or rbenv/rvm for version management"
        exit 1
    fi

    if command_exists gem; then
        print_success "gem (Ruby package manager) found"
    else
        print_error "gem not found"
        print_info "gem should be included with Ruby installation"
        exit 1
    fi
else
    print_error "Ruby not found"
    print_info "Ruby 2.7+ is required for cfn-nag (CloudFormation security scanning)"
    print_info "Install Ruby:"
    print_info "  - macOS: brew install ruby"
    print_info "  - Linux: Use your package manager (apt, yum, etc.)"
    print_info "  - Or use rbenv/rvm for version management"
    exit 1
fi

# Step 5: Install dependencies with uv
print_header "Step 5: Installing Python Dependencies"

if [ -f "pyproject.toml" ]; then
    print_info "Installing dependencies with uv sync..."

    # Install all dependencies including dev extras
    print_info "Installing all dependencies (production + development)..."
    uv sync --extra dev
    print_success "All dependencies installed"
else
    print_error "pyproject.toml not found"
    print_info "This project requires pyproject.toml for dependency management"
    exit 1
fi

# Step 6: Verify virtual environment
print_header "Step 6: Verifying Virtual Environment"

if [ -d ".venv" ]; then
    print_success "Virtual environment exists (.venv/)"
    print_info "All commands use 'uv run' — no manual activation needed"
else
    print_error "Virtual environment not found"
    print_info "uv sync should have created .venv automatically"
    exit 1
fi

# Step 7: Set up git commit template
print_header "Step 7: Configuring Git Commit Template"

if [ -f ".gitmessage" ]; then
    git config commit.template .gitmessage
    print_success "Git commit template configured"
    print_info "Template location: $(git config commit.template)"
else
    print_warning ".gitmessage file not found, skipping"
fi

# Step 8: Install pre-commit hooks
print_header "Step 8: Installing Pre-commit Hooks"

if [ -f ".pre-commit-config.yaml" ]; then
    # Reset system-level git hooks path (required for pre-commit to work)
    print_info "Resetting system-level git hooks configuration..."
    if sudo git config --system --unset-all core.hookspath 2>/dev/null; then
        print_success "System git hooks path reset"
    else
        print_warning "Could not reset system git hooks path (may require sudo password)"
        print_info "If pre-commit hooks don't work, run manually:"
        print_info "  sudo git config --system --unset-all core.hookspath"
    fi

    # Migrate config to fix deprecated stages
    print_info "Migrating pre-commit config (fixing deprecated stages)..."
    uv run pre-commit migrate-config || true

    print_info "Installing pre-commit hooks..."
    uv run pre-commit install
    uv run pre-commit install --hook-type commit-msg
    print_success "Pre-commit hooks installed"

    # Create detect-secrets baseline (only if one doesn't already exist with content)
    if [ ! -s ".secrets.baseline" ]; then
        print_info "Creating detect-secrets baseline..."
        if ! command_exists detect-secrets; then
            print_info "Installing detect-secrets..."
            uv pip install detect-secrets
        fi
        if uv run detect-secrets scan --exclude-files package-lock.json > .secrets.baseline; then
            print_success "detect-secrets baseline created"
        else
            print_error "detect-secrets scan failed"
            rm -f .secrets.baseline
            exit 1
        fi
    else
        print_success "detect-secrets baseline already exists"
    fi

    # Install git defender if available
    if command_exists git-defender; then
        print_info "Installing git defender..."
        if git defender --install 2>/dev/null; then
            print_success "Git defender installed"
        else
            print_warning "Git defender installation failed (optional, continuing)"
        fi
    else
        print_warning "git-defender not found (optional)"
        print_info "If you need git-defender, install it separately"
    fi

    print_info "Running pre-commit on all files (this may take a few minutes)..."
    if uv run pre-commit run --all-files; then
        print_success "All pre-commit checks passed"
    else
        print_warning "Some pre-commit checks failed (this is normal for first run)"
        print_info "Files have been auto-fixed where possible"
        print_info "Review changes and commit them"
    fi
else
    print_warning ".pre-commit-config.yaml not found, skipping"
fi

# Step 9: Verify setup
print_header "Step 9: Verifying Setup"

print_info "Checking Python packages..."
if python -c "import pydantic" 2>/dev/null; then
    print_success "pydantic package installed"
else
    print_warning "pydantic package not found (will be installed later)"
fi

if python -c "import pytest" 2>/dev/null; then
    print_success "pytest installed"
else
    print_warning "pytest not found"
fi

if python -c "import boto3" 2>/dev/null; then
    print_success "boto3 installed (required for AWS/RDS access)"
else
    print_warning "boto3 not found (required for RDS data collection)"
fi

print_info "Checking CloudFormation tools..."
if command_exists cfn-lint; then
    CFN_LINT_VERSION=$(cfn-lint --version 2>&1 | head -n 1)
    print_success "cfn-lint installed: $CFN_LINT_VERSION"
else
    print_warning "cfn-lint not found (required for CloudFormation validation)"
    print_info "Installing cfn-lint..."
    uv pip install cfn-lint
    if command_exists cfn-lint; then
        print_success "cfn-lint installed successfully"
    else
        print_error "cfn-lint installation failed"
    fi
fi

if command_exists cfn_nag_scan; then
    CFN_NAG_VERSION=$(cfn_nag_scan --version 2>&1 | head -n 1)
    print_success "cfn-nag installed: $CFN_NAG_VERSION"
else
    print_error "cfn-nag not found (required for CloudFormation security scanning)"
    print_info "Installing cfn-nag..."
    if gem install cfn-nag; then
        print_success "cfn-nag installed successfully"
    else
        print_error "cfn-nag installation failed"
        print_info "Please install manually: gem install cfn-nag"
        exit 1
    fi
fi

# Step 10: Run tests (optional)
print_header "Step 10: Running Tests (Optional)"

if [ -d "tests" ]; then
    read -p "Run test suite to verify setup? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        print_info "Running tests..."
        if pytest tests/ -v; then
            print_success "All tests passed"
        else
            print_warning "Some tests failed (this may be expected if implementation is incomplete)"
        fi
    else
        print_info "Skipping tests"
    fi
else
    print_warning "tests/ directory not found, skipping"
fi

# Step 11: Summary
print_header "Setup Complete!"

echo -e "${GREEN}Your development environment is ready!${NC}\n"

echo "Next steps:"
echo "  1. Activate virtual environment:"
echo -e "     ${BLUE}source .venv/bin/activate${NC}"
echo ""
echo "  2. Configure AWS credentials (required for RDS access):"
echo -e "     ${BLUE}aws configure${NC}"
echo ""
echo "  3. Start development:"
echo -e "     ${BLUE}git checkout -b feature/your-feature${NC}"
echo ""
echo "  4. Make changes and commit:"
echo -e "     ${BLUE}git commit${NC}  (template will open in editor)"
echo ""
echo "  5. Run tests:"
echo -e "     ${BLUE}pytest tests/${NC}"
echo ""
echo "  6. Validate contracts:"
echo -e "     ${BLUE}python scripts/validate_contracts.py docs/03-contracts/models/*.py${NC}"
echo ""

print_info "Pre-commit hooks will run automatically on git commit"
print_info "Commit template will open in your editor when you run 'git commit'"
print_info "Contracts use Pydantic models for type-safe validation"
print_info "See CONTRIBUTING.md for full development guidelines"

echo -e "\n${GREEN}Happy coding! 🚀${NC}\n"

# Database Modernizer Assessment - Release Management Guide

**Version:** 2.0
**Date:** February 4, 2026
**Owner:** Database Modernizer Assessment Engineering Team

---

## Overview

This document describes the release management process for Database Modernizer Assessment, including version numbering, git workflow, release procedures, and hotfix processes.

**Repository Structure:** Monorepo (single repository for backend, frontend, contracts, infrastructure)

**Related ADRs:**

- [ADR-010: Release Management and Version Control](02-architecture/decisions/ADR-010-release-management.md)
- [ADR-011: Monorepo Structure](02-architecture/decisions/ADR-011-monorepo-structure.md)

---

## Monorepo Release Strategy

### Single Repository, Single Release

Database Modernizer Assessment uses a **monorepo structure** where all components are released together:

**Components in Single Release:**

- Backend (Python): Agents, API, orchestrator
- Frontend (TypeScript/React): Web UI
- Contracts (Pydantic): Shared models
- Infrastructure (CloudFormation/Docker): Deployment configs
- Documentation: All guides and specs

**Release Artifact:**

- Single GitHub release tag (e.g., `v1.0.0`)
- Multiple Docker images (backend, frontend, worker)
- Single deployment package for customers

**Customer Experience:**

```bash
# Single clone
git clone https://github.com/aws/database-modernizer.git
cd database-modernizer

# Single setup
./scripts/setup_dev.sh

# Single deployment
docker-compose up  # Or: aws cloudformation deploy
```

### Why Monorepo?

See [ADR-011: Monorepo Structure](02-architecture/decisions/ADR-011-monorepo-structure.md) for complete rationale.

**Key Benefits:**

- ✅ Contracts always in sync between backend and frontend
- ✅ Atomic commits across full stack
- ✅ Simpler customer onboarding (one repo to clone)
- ✅ Single release process
- ✅ Easier for small team to manage

---

## Semantic Versioning

### Version Format: MAJOR.MINOR.PATCH

**MAJOR version** (v1.0.0 → v2.0.0):

- Breaking changes for users
- Incompatible API changes
- Major architectural changes
- Requires user action (migration, config changes)

**MINOR version** (v1.5.0 → v1.6.0):

- New features (backward compatible)
- New analysis types
- New target platforms
- Deprecations (with warnings)

**PATCH version** (v1.5.0 → v1.5.1):

- Bug fixes
- Security patches
- Performance improvements
- Documentation updates

---

## Git Workflow

### Branch Strategy

```
mainline (default branch)
    ↓
    ├─ feature/add-postgresql-collector
    ├─ feature/aurora-analysis
    ├─ bugfix/mysql-timeout
    └─ release/v1.5.x (created for maintenance)
         └─ hotfix/critical-security-fix
```

### Branch Types

**mainline** (long-lived)

- Default branch for all development
- Always deployable
- Protected (requires PR + reviews)

**feature/*** (short-lived)

- New features or enhancements
- Branch from: mainline
- Merge to: mainline (via PR)
- Delete after merge

**bugfix/*** (short-lived)

- Bug fixes
- Branch from: mainline
- Merge to: mainline (via PR)
- Delete after merge

**release/vX.Y.x** (long-lived, on-demand)

- Maintenance branch for released version
- Created when first hotfix needed
- Branch from: release tag

**hotfix/*** (short-lived)

- Emergency fixes for released versions
- Branch from: release/vX.Y.x
- Merge to: release/vX.Y.x AND mainline
- Delete after merge

---

## Release Process

### Standard Release (MINOR or PATCH)

**Step 1: Development**

```bash
# Create feature branch
git checkout -b feature/add-postgresql-collector mainline

# Develop and commit
git commit -m "feat(collector): add PostgreSQL collector"

# Create PR to mainline
# After review and approval, merge
```

**Step 2: Prepare Release**

```bash
# Update version in code
# - setup.py
# - package.json
# - __version__.py

# Update CHANGELOG.md (automated from commits)
git log v1.4.0..HEAD --pretty=format:"%s" | grep -E "^(feat|fix)"

# Create release PR
git checkout -b release-prep/v1.5.0 mainline
git commit -m "chore: prepare release v1.5.0"
# Create PR, review, merge
```

**Step 3: Tag Release**

```bash
# Tag on mainline
git checkout mainline
git pull
git tag -a v1.5.0 -m "Release v1.5.0: Add PostgreSQL collector and Aurora analysis"
git push origin v1.5.0
```

**Step 4: Build and Deploy**

```bash
# CI/CD automatically builds release artifacts
# Deploy to staging
# Run smoke tests
# Deploy to production (user downloads)
```

**Step 5: Create Release Branch (Optional)**

```bash
# Only if hotfixes expected
git checkout -b release/v1.5.x v1.5.0
git push origin release/v1.5.x
```

---

## Hotfix Process

### Scenario: Critical bug in v1.5.0

**Step 1: Create Release Branch (if doesn't exist)**

```bash
git checkout -b release/v1.5.x v1.5.0
git push origin release/v1.5.x
```

**Step 2: Create Hotfix Branch**

```bash
git checkout -b hotfix/critical-security-fix release/v1.5.x
```

**Step 3: Fix the Bug**

```bash
# Make changes
# Add tests
git commit -m "fix(security): patch SQL injection vulnerability

Fixes #789
Security: CVE-2026-12345"
```

**Step 4: Merge to Release Branch**

```bash
# Create PR to release/v1.5.x
# Review and merge

# Tag new patch version
git checkout release/v1.5.x
git pull
git tag -a v1.5.1 -m "Hotfix v1.5.1: Security patch"
git push origin v1.5.1
```

**Step 5: Cherry-Pick to Mainline**

```bash
git checkout mainline
git cherry-pick <commit-hash>
git push origin mainline

# Or create PR from hotfix branch to mainline
```

**Step 6: Deploy Hotfix**

```bash
# CI/CD builds v1.5.1
# Deploy to production
```

---

## Conventional Commits

### Format

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Types

- **feat**: New feature
- **fix**: Bug fix
- **docs**: Documentation changes
- **style**: Code style changes
- **refactor**: Code refactoring
- **perf**: Performance improvements
- **test**: Test changes
- **chore**: Build/tooling changes
- **ci**: CI/CD changes

### Examples

```bash
# Feature
git commit -m "feat(collector): add PostgreSQL collector

Implement PostgreSQL collector with support for:
- Schema collection
- Query pattern analysis
- Performance Insights integration

Closes #123"

# Bug fix
git commit -m "fix(mysql): handle connection timeout gracefully

MySQL connections now retry with exponential backoff.

Fixes #456"

# Breaking change
git commit -m "feat(api)!: remove deprecated endpoints

BREAKING CHANGE: Removed /v1/analyze endpoint.
Use /v2/analyze instead.

Migration guide: docs/migration/v1-to-v2.md"
```

---

## Release Notes

### Template

```markdown
# Release v1.5.0

**Release Date:** 2026-02-15

## 🎉 New Features

- **PostgreSQL Collector**: Added support for PostgreSQL databases
- **Aurora Analysis**: New analysis agent for Aurora-specific recommendations

## 🐛 Bug Fixes

- **MySQL Collector**: Fixed connection timeout handling
- **Analysis Agent**: Fixed null pointer in schema analysis

## ⚡ Performance Improvements

- **Collector**: Optimized schema collection for large databases (8x faster)

## 📚 Documentation

- Added PostgreSQL collector guide
- Updated Aurora analysis documentation

## 🔄 Dependency Updates

- Strands SDK: 1.2.0 → 1.3.0
- Pydantic: 2.5.0 → 2.6.0

## 🔗 Links

- [Full Changelog](https://github.com/org/modernizer/compare/v1.4.0...v1.5.0)
- [Migration Guide](docs/migration/v1.4-to-v1.5.md)
```

### Generation

```bash
# Generate release notes from commits
git log v1.4.0..v1.5.0 --pretty=format:"%s" | \
  grep -E "^(feat|fix|perf|refactor)" | \
  sort
```

---

## Cherry-Picking Policy

### When to Cherry-Pick

**✅ Always cherry-pick:**

- Hotfixes (security, critical bugs)
- Security patches

**❌ Never cherry-pick:**

- New features (use MINOR release)
- Refactoring (too risky)
- Non-critical changes

### Process

```bash
# Find commit to cherry-pick
git log release/v1.5.x

# Cherry-pick to mainline
git checkout mainline
git cherry-pick <commit-hash>

# If conflicts, resolve and continue
git cherry-pick --continue

# Push
git push origin mainline
```

---

## Branch Protection Rules

### mainline Branch

**Required:**

- ✅ Pull request before merge
- ✅ At least 1 approval
- ✅ Status checks pass (CI/CD)
- ✅ Conversation resolved
- ✅ Up-to-date with base branch

**Prohibited:**

- ❌ Direct commits
- ❌ Force push
- ❌ Branch deletion

### release/* Branches

**Required:**

- ✅ Pull request before merge
- ✅ At least 1 approval
- ✅ Status checks pass

**Allowed:**

- ✅ Hotfix merges only
- ✅ Cherry-picks from mainline (if needed)

---

## Version Mapping

### Release Version → Contract Versions

```
Release v1.5.0 (user-facing)
  ├─ CollectorOutput: contract v1.2
  ├─ AnalysisOutput: contract v1.0
  └─ ModernizationReport: contract v1.0

Release v2.0.0 (user-facing, breaking)
  ├─ CollectorOutput: contract v2.0 (breaking)
  ├─ AnalysisOutput: contract v2.0 (breaking)
  └─ ModernizationReport: contract v2.0 (breaking)
```

**Note:** Release MAJOR version bump usually (but not always) means contract MAJOR version bump.

---

## Quick Reference

### Create Feature Branch

```bash
git checkout -b feature/my-feature mainline
```

### Create Release

```bash
# 1. Merge all features to mainline
# 2. Update version and CHANGELOG
# 3. Tag release
git tag -a v1.5.0 -m "Release v1.5.0"
git push origin v1.5.0
```

### Create Hotfix

```bash
# 1. Create/checkout release branch
git checkout -b release/v1.5.x v1.5.0

# 2. Create hotfix branch
git checkout -b hotfix/fix-issue release/v1.5.x

# 3. Fix, commit, merge to release branch
# 4. Tag patch version
git tag -a v1.5.1 -m "Hotfix v1.5.1"

# 5. Cherry-pick to mainline
git checkout mainline
git cherry-pick <commit-hash>
```

---

## Related Documents

- [ADR-010: Release Management and Version Control](02-architecture/decisions/ADR-010-release-management.md)
- [ADR-008: Contract Versioning](02-architecture/decisions/ADR-008-contract-versioning.md)
- [Conventional Commits Specification](https://www.conventionalcommits.org/)
- [Semantic Versioning Specification](https://semver.org/)

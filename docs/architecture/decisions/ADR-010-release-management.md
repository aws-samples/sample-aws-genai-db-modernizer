# ADR-010: Release Management and Version Control Strategy

**Status:** Accepted
**Date:** 2026-02-02
**Deciders:** Architecture Team
**Related ADRs:** ADR-008 (Contract Versioning)

---

## Context

Database Modernizer needs a clear release management strategy for:

- **Version numbering**: User-facing release versions
- **Git workflow**: Branch strategy and commit practices
- **Release tagging**: Git tags for releases
- **Hotfix process**: Emergency fixes to production
- **Cherry-picking**: Backporting fixes to release branches
- **Release notes**: Communicating changes to users

### Key Distinction

**Release versions** (user-facing) are separate from **contract versions** (internal):

- Release version: v1.5.0 (what users see)
- Contract versions: 1.2, 1.0, 1.0 (internal data structure versions)

### Requirements

- **Semantic versioning**: Clear version numbering for releases
- **Git workflow**: Simple, maintainable branch strategy
- **Automated tagging**: Consistent tag format
- **Hotfix support**: Fast path for critical fixes
- **Release notes**: Automated generation from commits
- **Backward compatibility**: Clear breaking change communication

---

## Decision

We will implement **Semantic Versioning with Git Flow (Simplified)**:

1. **Semantic Versioning** for releases (MAJOR.MINOR.PATCH)
2. **Mainline-based development** (single long-lived branch)
3. **Release branches** for maintenance (created on-demand)
4. **Git tags** for releases (v1.5.0, v2.0.0)
5. **Conventional Commits** for automated release notes
6. **Hotfix branches** for emergency fixes

---

## Semantic Versioning for Releases

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

### Examples

```
v1.0.0 - Initial release
v1.1.0 - Add PostgreSQL collector
v1.1.1 - Fix MySQL connection timeout
v1.2.0 - Add Aurora analysis agent
v2.0.0 - Breaking: New input contract format
```

---

## Git Workflow

### Branch Strategy: Mainline-Based Development

```
main (default branch)
    ↓
    ├─ feature/add-postgresql-collector
    ├─ feature/aurora-analysis
    ├─ bugfix/mysql-timeout
    └─ release/v1.5.x (created for maintenance)
         └─ hotfix/critical-security-fix
```

### Branch Types

**1. main** (long-lived)

- Default branch for all development
- Always deployable
- Protected (requires PR + reviews)
- Source of truth

**2. feature/* (short-lived)**

- New features or enhancements
- Branch from: main
- Merge to: main (via PR)
- Naming: `feature/description` (e.g., `feature/add-postgresql-collector`)
- Delete after merge

**3. bugfix/* (short-lived)**

- Bug fixes
- Branch from: main
- Merge to: main (via PR)
- Naming: `bugfix/description` (e.g., `bugfix/mysql-timeout`)
- Delete after merge

**4. release/vX.Y.x** (long-lived, created on-demand)

- Maintenance branch for released version
- Created when first hotfix needed
- Branch from: release tag (e.g., v1.5.0)
- Naming: `release/v1.5.x`, `release/v2.0.x`
- Keep for supported versions only

**5. hotfix/* (short-lived)**

- Emergency fixes for released versions
- Branch from: release/vX.Y.x
- Merge to: release/vX.Y.x AND main (cherry-pick)
- Naming: `hotfix/description` (e.g., `hotfix/critical-security-fix`)
- Delete after merge

---

## Git Tagging Strategy

### Tag Format

**Release tags:**

```
v1.0.0
v1.5.0
v2.0.0
```

**Pre-release tags:**

```
v1.5.0-rc.1
v1.5.0-beta.1
v2.0.0-alpha.1
```

### Tagging Process

```bash
# Tag a release (annotated tag)
git tag -a v1.5.0 -m "Release v1.5.0: Add Aurora analysis"
git push origin v1.5.0

# Tag a pre-release
git tag -a v1.5.0-rc.1 -m "Release candidate 1 for v1.5.0"
git push origin v1.5.0-rc.1
```

### Tag Metadata

Annotated tags include:

- Version number
- Release date
- Release notes summary
- Committer information

---

## Release Process

### Standard Release (MINOR or PATCH)

```
1. Development on main
   ├─ feature/add-postgresql-collector (merged)
   ├─ feature/aurora-analysis (merged)
   └─ bugfix/mysql-timeout (merged)

2. Prepare release
   ├─ Update version in code (pyproject.toml, package.json, etc.)
   ├─ Update CHANGELOG.md (automated from commits)
   └─ Create PR: "Release v1.5.0"

3. Merge release PR to main

4. Tag release
   └─ git tag -a v1.5.0 -m "Release v1.5.0"

5. Build and deploy
   ├─ CI/CD builds release artifacts
   ├─ Deploy to staging
   ├─ Smoke tests
   └─ Deploy to production (user downloads)

6. Create release branch (if needed for hotfixes)
   └─ git checkout -b release/v1.5.x v1.5.0
```

### Major Release (MAJOR)

Same as standard release, plus:

- Migration guide for breaking changes
- Deprecation warnings in previous MINOR version
- Extended testing period
- Communication plan for users

---

## Hotfix Process

### Scenario: Critical bug in v1.5.0

```
1. Create release branch (if doesn't exist)
   git checkout -b release/v1.5.x v1.5.0

2. Create hotfix branch
   git checkout -b hotfix/critical-security-fix release/v1.5.x

3. Fix the bug
   ├─ Make changes
   ├─ Add tests
   └─ Commit with conventional commit format

4. Merge to release branch
   ├─ Create PR to release/v1.5.x
   ├─ Review and merge
   └─ Tag new patch version: v1.5.1

5. Cherry-pick to main
   git checkout main
   git cherry-pick <commit-hash>
   # Or create PR from hotfix branch to main

6. Deploy hotfix
   ├─ Build v1.5.1
   └─ Deploy to production
```

---

## Cherry-Picking Policy

### When to Cherry-Pick

**✅ Cherry-pick from release branch to main:**

- Hotfixes (always)
- Critical bug fixes
- Security patches

**❌ Don't cherry-pick from main to release branch:**

- New features (use MINOR release instead)
- Refactoring (too risky)
- Non-critical changes

### Cherry-Pick Process

```bash
# Find commit to cherry-pick
git log release/v1.5.x

# Cherry-pick to main
git checkout main
git cherry-pick <commit-hash>

# If conflicts, resolve and continue
git cherry-pick --continue

# Push to main
git push origin main
```

### Cherry-Pick Guidelines

1. **Always cherry-pick hotfixes to main** (prevent regression)
2. **Test after cherry-pick** (may behave differently on main)
3. **Document in commit message** (e.g., "Cherry-picked from v1.5.1")
4. **Prefer forward-porting** (release → main, not main → release)

---

## Conventional Commits

### Commit Message Format

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Types

- **feat**: New feature
- **fix**: Bug fix
- **docs**: Documentation changes
- **style**: Code style changes (formatting)
- **refactor**: Code refactoring
- **perf**: Performance improvements
- **test**: Test changes
- **chore**: Build/tooling changes
- **ci**: CI/CD changes

### Examples

```
feat(collector): add PostgreSQL collector agent

Implement PostgreSQL collector with support for:
- Schema collection
- Query pattern analysis
- Performance Insights integration

Closes #123

---

fix(mysql): handle connection timeout gracefully

MySQL connections now retry with exponential backoff
when encountering timeout errors.

Fixes #456

---

feat(analysis)!: breaking change to analysis output contract

BREAKING CHANGE: AnalysisOutput contract changed from v1.0 to v2.0.
Field 'recommendations' renamed to 'scored_recommendations'.

Migration guide: docs/migration/v1-to-v2.md
```

### Breaking Changes

Mark breaking changes with `!` or `BREAKING CHANGE:` in footer:

```
feat(api)!: remove deprecated endpoints

BREAKING CHANGE: Removed /v1/analyze endpoint.
Use /v2/analyze instead.
```

---

## Release Notes Generation

### Automated from Conventional Commits

```bash
# Generate release notes from commits
git log v1.4.0..v1.5.0 --pretty=format:"%s" | \
  grep -E "^(feat|fix|perf|refactor)" | \
  sort

# Output:
# feat(collector): add PostgreSQL collector
# feat(analysis): add Aurora analysis agent
# fix(mysql): handle connection timeout
# perf(collector): optimize schema collection for large databases
```

### Release Notes Template

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

- [Full Changelog](https://github.com/aws-samples/aws-genai-db-modernizer/compare/v1.4.0...v1.5.0)
- [Migration Guide](docs/migration/v1.4-to-v1.5.md)
```

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

**Key Point:** Release MAJOR version bump usually (but not always) means contract MAJOR version bump.

---

## Branch Protection Rules

### main Branch

**Required:**

- ✅ Pull request before merge
- ✅ At least 1 approval
- ✅ Status checks pass (CI/CD)
- ✅ Conversation resolved
- ✅ Up-to-date with base branch

**Prohibited:**

- ❌ Direct commits (except release tags)
- ❌ Force push
- ❌ Branch deletion

### release/* Branches

**Required:**

- ✅ Pull request before merge
- ✅ At least 1 approval
- ✅ Status checks pass

**Allowed:**

- ✅ Hotfix merges only
- ✅ Cherry-picks from main (if needed)

---

## Consequences

### Positive

✅ **Clear versioning**: Semantic versioning easy to understand
✅ **Simple workflow**: Mainline-based development (no long-lived feature branches)
✅ **Hotfix support**: Fast path for critical fixes
✅ **Automated release notes**: Generated from conventional commits
✅ **Backward compatibility**: Clear breaking change communication
✅ **Cherry-pick policy**: Clear guidelines for backporting

### Negative

⚠️ **Release branch maintenance**: Need to maintain multiple release branches
⚠️ **Cherry-pick overhead**: Manual process for backporting fixes
⚠️ **Commit discipline**: Requires conventional commit format

### Neutral

🔶 **Mainline-based**: Simpler than Git Flow (no develop branch)
🔶 **Release branches on-demand**: Created only when hotfixes needed
🔶 **Tag-based releases**: Tags mark release points

---

## Alternatives Considered

### Alternative 1: Git Flow (Rejected)

**Rejected because:**

- ❌ Too complex (develop + main + feature + release + hotfix)
- ❌ Overhead for small team
- ❌ Mainline-based simpler

### Alternative 2: Trunk-Based Development (Rejected)

**Rejected because:**

- ❌ No release branches (hard to hotfix old versions)
- ❌ Requires feature flags (added complexity)
- ❌ Need to support multiple versions

### Alternative 3: GitHub Flow (Rejected)

**Rejected because:**

- ❌ No release branches (can't hotfix old versions)
- ❌ Too simple for versioned releases
- ❌ Need to maintain multiple versions

---

## Related Documents

- [ADR-008: Contract Versioning](ADR-008-contract-versioning.md)
- [Conventional Commits Specification](https://www.conventionalcommits.org/)
- [Semantic Versioning Specification](https://semver.org/)

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-02 | Architecture Team | Initial decision |

---

**Status: Accepted and Ready for Implementation**

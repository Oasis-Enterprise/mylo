# Changelog

All notable changes to Mylo will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial project scaffold (Milestone 0).
- Add-on manifest, Dockerfile, Python package skeleton, CI workflow.

### Changed
- Collapsed `addon/` subdirectory into the repo root. HA Supervisor uses the
  add-on directory as the Docker build context, so the Dockerfile couldn't
  reach `../src/` or `../pyproject.toml`. Single-add-on layout (`config.yaml`
  at root) is the correct pattern here. `repository.yaml` removed.

# exoplanet-db

AI agent-friendly, lightweight exoplanet database and public portal with first-class reproducibility tracking, a Model Context Protocol (MCP) server, multi-source ingestion, homelab deployment on `marica`, and an observation candidate priority queue.

## Features

- **Agent-First REST & MCP Surfaces**: Clean structured JSON API and Model Context Protocol (Streamable HTTP + stdio) tailored for LLM agents (Antigravity, Cursor, Claude).
- **First-Class Reproducibility**: Tracks independent signals, verification receipts, pipeline versions, and reproducibility tiers.
- **Epistemic Honesty**: Dispositions (`unclassified_transit_like_signal`, `known_signal`, `planet_candidate`, `validated_planet`, `false_positive`), source localisation, and scientific limits are mandatory first-class fields on every candidate. `check_novelty` returns verifiable evidentiary snapshots rather than booleans.
- **Multi-Source Ingestion**:
  - NASA Exoplanet Archive TAP (`ps` individual references as Reports, `pscomppars` composite view, `toi`).
  - ExoFOP TOIs & Community TOIs (CTOIs).
  - Curated community discoveries (`eumemic/exoplanets` CC BY 4.0).
  - Versioned Git candidate dossiers (`brahle/exoplanet-candidates`).
- **Coherent Ephemeris & Priority Scheduling**: Predicts topocentric transit ingress/egress in UTC via Astropy barycentric corrections ($\pm 8.3\text{ min}$) and propagates timing uncertainty using full covariance matrices $\sigma_T(N)^2 = \sigma_{T_0}^2 + N^2 \sigma_P^2 + 2N \cdot \text{cov}(T_0, P)$. Strictly rejects composite parameter mixtures.
- **Homelab Production Ready**: Deployed on `marica` via MicroK8s, CloudNativePG (CNPG) PostgreSQL, Vault secrets, MinIO evidence storage, Traefik rate limiting, and ArgoCD GitOps.

## Local Setup

```bash
uv sync --all-extras
uv run pytest tests/unit/
```

## Attribution & Licenses

- Project code: [MIT License](LICENSE).
- NASA Exoplanet Archive data: Courtesy of NASA Exoplanet Archive / Caltech-IPAC.
- Community catalogue data from `eumemic/exoplanets`: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

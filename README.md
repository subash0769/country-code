# Country Explorer: GitOps Walkthrough

A single-file Flask app: search any country and see its flag, capital, population, languages, currencies, timezones and neighbours.
Data comes from restcountries.com (free, no API key).

- Port **8000**
- `/` page, `/api/country?name=Nepal` JSON, `/healthz` for probes
- Footer shows the **pod name and version**, so you can watch load balancing and rolling updates.

## Two repos

| Repo | Contains |
|---|---|
| `country-app` (app repo) | `app.py`, `requirements.txt`, `Dockerfile`, `.github/workflows/cicd.yml` |
| `Country-infra` (manifest repo) | `country/` folder: `namespace.yaml`, `deployment.yaml`, `service.yaml`, `httproute.yaml` |

## Suggested names

| Thing | Value |
|---|---|
| Image | `<dockerhub-user>/country-app` |
| Namespace | `country-app` |
| Deployment / label | `country` / `app: country` |
| Service | `country-service` (port 8000 -> 8000) |
| Hostname | `country.local` |
| Argo CD app | `country-infra`, path `country`, destination namespace `country-app` |

## Process

1. **Run locally**
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python app.py          # http://localhost:8000
   ```
2. **Dockerfile** (`COPY app.py .`, run `gunicorn -b 0.0.0.0:8000 app:app`), then build, test and push:
   ```bash
   docker build -t <dockerhub-user>/country-app:1.0 .
   docker run --rm -p 8000:8000 <dockerhub-user>/country-app:1.0
   docker push <dockerhub-user>/country-app:1.0
   ```
3. **Manifests** in the infra repo under `country/`:
   - Deployment: 2 replicas, `containerPort: 8000`, liveness and readiness probes on `/healthz`, modest resources.
   - Service: `port: 8000`, `targetPort: 8000`, selector `app: country`.
   - HTTPRoute: `parentRefs` = `traefik-gateway` in namespace `traefik`; `hostnames` = `country.local`; `backendRefs` = `country-service`, port `8000`.
4. **Cluster prerequisites** (already done once): Gateway API CRDs (v1.7+), Traefik via Helm with the Gateway provider on, Gateway `allowedRoutes.namespaces.from: All`, Argo CD installed in `argocd` with `server.insecure: "true"`.
5. **Argo CD application**: project `default`, automatic sync, auto-create namespace, repo `Country-infra`, revision `main`, path `country`, destination `https://kubernetes.default.svc` / namespace `country-app`.
6. **GitHub Actions**: copy the previous `cicd.yml` into the app repo at `.github/workflows/cicd.yml` and change:
   - the image name to `country-app`
   - `repository:` to `<github-user>/Country-infra`
   - the file path to `country/deployment.yaml`
   - the `sed` pattern to match `/country-app`
   Secrets in the **app repo**: `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`, `INFRA_REPO_TOKEN`.
7. **Access locally**:
   ```bash
   kubectl port-forward -n traefik svc/traefik 8081:80
   echo "127.0.0.1 country.local" | sudo tee -a /etc/hosts
   # open http://country.local:8081
   ```

## Try the loop
Change the footer text in `app.py` and push. The workflow builds a new image and updates the tag in the infra repo. Argo CD syncs it, and the **pod name in the footer changes** as the rollout completes.

## Keep the infra folder clean
Only put this app's manifests under `country/`. Anything else in that folder (such as an Argo CD Ingress) gets managed by the same Argo application.

## Quick troubleshooting

| Symptom | Check |
|---|---|
| Argo CD "project does not exist" | Use `default` as the project |
| HTTPRoute not attaching | Gateway `allowedRoutes` is `All`; parentRef name and namespace are correct |
| 404 from Traefik | Host header equals `country.local`; port-forward is running on the right port |
| 503 | `kubectl get endpoints country-service -n country-app` is empty: probes failing or labels differ |
| Page says "couldn't reach the country data service" | Pods have no outbound internet access |
# country-code

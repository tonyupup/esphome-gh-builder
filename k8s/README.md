# Deployment (k3s, managed by Flux)

The live manifests are **not** in this repo. They live in the cluster's GitOps repo
(`tony/k3s-gitops`, `clusters/alioth/esphome/`): `kubectl patch` on a Flux-managed resource is
reverted by the kustomize-controller, so change it there.

- `deployment.yaml` — the `esphome` Deployment: `git-sync`, `esphome` (Device Builder dashboard) and
  the `gh-builder` sidecar (this project's image).
- `gh-builder.yaml` — PVC `esphome-gh-builder-data` (identity key + pairings; survives pod restarts).

## Network
The pod has no `hostNetwork`. A Multus ipvlan L2 interface (NAD `lan-ipvlan`, master `wlan0`) gives it a
LAN address, `172.18.1.50/24`; the pod keeps `eth0` (flannel) as its default route.
All containers share that address and are told apart by port:

| Port | Who |
|---|---|
| 6052 | Device Builder UI |
| 6055 | the dashboard's own build-server (peer-link) listener |
| 6066 | **gh-builder** build server — pair offloading dashboards with `172.18.1.50:6066` |

ipvlan L2 limitation: the node that runs the pod cannot reach the pod's LAN address itself; other LAN
hosts can.

## Secrets (created by hand, not in git)
- `esphome-gh-token` — key `GH_TOKEN`: fine-grained PAT for `tonyupup/esphome-gh-builder`
  (Contents RW, Actions RW, Metadata R).
- `esphome-s3` — keys `KEY`, `SECRET` for the result cache bucket.

## First pairing
The sidecar opens a 15-minute pairing window on first start and prints the fingerprint and a one-time key:

    kubectl logs deploy/esphome -n default -c gh-builder | grep -A12 "REMOTE BUILD PAIRING"

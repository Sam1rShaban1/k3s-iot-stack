# Cluster recovery runbook

Last known good state, before the nodes dropped off the network: 15/15 preflight
checks passing, 156 tests passing, `make lint` clean, pushed at `6259ff0`.

## Symptom

Nothing in the cluster answers on the LAN. A sweep of `192.168.1.0/24` returns
only the router (`.1`), this workstation (`.23`) and three unrelated hosts.

    ping 192.168.1.136      -> 100% packet loss
    ssh master@192.168.1.50 -> no route / connection refused
    kubectl                 -> dial tcp 192.168.1.50:6443: no route to host

## Why `kubectl` fails first

`~/.kube/config` names the control-plane node by **LAN address**
(`https://192.168.1.50:6443`), and those addresses come from **DHCP**. The
addresses have already moved once during this work — `raspberrypi`'s wlan0 was
`192.168.1.50`, later `192.168.1.162`, and the broker was last seen at
`192.168.1.136`. So even with the cluster healthy, `kubectl` breaks whenever the
lease changes, while `harness/config.py` survives because it probes candidates.

This is worth fixing properly rather than re-pointing by hand each time. Either:

- give the control-plane node a **static DHCP lease** on the router, or
- run the API server on a fixed address and rewrite the kubeconfig server from
  `config.py` at startup.

## Order of recovery

1. **Power.** Check whether the five Pis have power and whether their activity
   LEDs are on. If they are dark, this is a supply or strip problem, not a
   network one.
2. **Network.** The nodes reach the LAN over `wlan0` (WiFi). If they have power
   but no LAN presence, suspect the WiFi association rather than DHCP — a Pi
   that has lost its association keeps its lease for a while and then disappears
   from the sweep entirely, which matches what was observed.
3. **Reach one node.** Once any single node answers:

       ssh master@<new-lan-ip>

   From there, `kubectl` can be re-pointed at that node if it is the
   control-plane node (`10.0.0.1` internally).
4. **Repair the kubeconfig**, then confirm:

       kubectl get nodes            # expect 5 Ready
       python3 -m harness preflight # expect 15/15
5. **Re-establish the node IPs.** `ansible/inventory.ini` records the *internal*
   `10.0.0.x` addresses, which are static and configured on the nodes themselves.
   The LAN addresses are DHCP and are not pinned anywhere except the kubeconfig
   and `harness/config.py` fallbacks.

## What does not need doing

The data-plane work is committed and was verified working at the point of
loss: 100% delivery at 2000 and 3000 msg/s, lossless ingest, all six
NetworkPolicies genuinely enforced, Prometheus on a PVC with 30/30 scrape
targets. Nothing in the repository needs to be rolled back.

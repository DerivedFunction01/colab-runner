# state/transient — restore mirror (untracked)

Everything in this directory except this file is gitignored restore state:
copies of files whose canonical homes may not survive a VM restart
(the Colab CLI's OAuth token and session state today).

The registry in `config/transient.json` defines, per item, what is mirrored,
where it belongs, its permissions, its conflict rule, and how a restore is
verified. `ops/mirror_transient.py` performs capture and restore.
Contents are never committed, printed, or logged.

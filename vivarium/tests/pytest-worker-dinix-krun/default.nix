# pytest-worker-dinix with the guest under krun: its own kernel booted
# straight into dinit.
args: import ../pytest-worker-dinix (args // { backend = "krun"; })

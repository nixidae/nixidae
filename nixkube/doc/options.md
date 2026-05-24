## nixkube\.enable



Whether to enable nixkube\.



*Type:*
boolean



*Default:*

```nix
false
```



*Example:*

```nix
true
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.deploySecrets

Deploy SSH keypair Secrets to Kubernetes\. Disable if managing secrets externally (e\.g\., with Vault or Sealed Secrets)\.



*Type:*
boolean



*Default:*

```nix
true
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.hostMountPath



Where on the host to put nixkube store, / is untested and not recommended



*Type:*
absolute path



*Default:*

```nix
"/var/lib/nix-csi"
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.knownHosts



SSH host keys to accept when connecting to cache\.
Keys are written to known_hosts on nodes so they can connect without interactive verification\.



*Type:*
attribute set of (string or absolute path)



*Default:*

```nix
{ }
```



*Example:*

```nix
{
  "nix-cache" = "ssh-ed25519 AAAA...";
}

```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.loggingConfig



Logging configuration for the nixkube service (structlog-based)\.



*Type:*
submodule



*Default:*

```nix
{ }
```



*Example:*

```nix
# JSON renderer (default) — production/Loki
{
  renderer = "json";
  loggers.nixkube.level = "DEBUG";
  root.level = "WARNING";
}

# Logfmt renderer — stern / grep-friendly
{
  renderer = "logfmt";
  loggers.nixkube.level = "INFO";
}

# Console renderer — local development
{
  renderer = "console";
  loggers.nixkube.level = "DEBUG";
  root.level = "DEBUG";
}

```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.loggingConfig\.loggers



Per-logger level overrides\. Keys are Python logger names (dotted hierarchy)\.
All loggers under ` nixkube.* ` inherit from ` nixkube ` unless individually overridden\.



*Type:*
attribute set of (submodule)



*Default:*

```nix
{
  httpx = {
    level = "WARNING";
  };
  nixkube = {
    level = "INFO";
  };
  "nixkube.nix_daemon" = {
    level = "WARNING";
  };
}
```



*Example:*

```nix
{
  "nixkube".level = "DEBUG";
  "nixkube.nri".level = "DEBUG";
  "httpx".level = "ERROR";
}

```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.loggingConfig\.loggers\.\<name>\.level



Log level for this logger\.



*Type:*
one of “DEBUG”, “INFO”, “WARNING”, “ERROR”, “CRITICAL”

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.loggingConfig\.renderer



Log output renderer:

 - ` "json" ` (default): Structured JSON, one object per line\. Recommended
   for production and log aggregation (Loki, ELK, Datadog)\. Each
   structured field is a top-level JSON key, enabling rich queries:
   
   ```
   {app="nixkube"} | json | elapsed_time > 10
   {app="nixkube"} | json | returncode != 0
   {app="nixkube"} | json | container_id =~ "abc"
   ```

 - ` "logfmt" `: ` key=value ` pairs on a single line\. Human-readable and
   machine-parseable\. Works well with ` stern `, ` kubectl logs | grep `,
   and log shippers with native logfmt support (Vector, Fluentd)\.
   Example line:
   
   ```
   level=info logger=nixkube.nri event=build_task_completed container_id=abc123
   ```

 - ` "console" `: Coloured, aligned output for local development\.
   Not suitable for log aggregation or machine parsing\.



*Type:*
one of “json”, “logfmt”, “console”



*Default:*

```nix
"json"
```



*Example:*

```nix
"logfmt"
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.loggingConfig\.root



Root logger configuration (catch-all for third-party libraries)\.



*Type:*
submodule



*Default:*

```nix
{ }
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.loggingConfig\.root\.level



Root logger level\. All loggers inherit this unless overridden in ` loggers `\.



*Type:*
one of “DEBUG”, “INFO”, “WARNING”, “ERROR”, “CRITICAL”



*Default:*

```nix
"WARNING"
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.metadata



Metadata (labels, annotations) applied to nixkube resources



*Type:*
JSON value



*Default:*

```nix
{ }
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.namespace



Which namespace to deploy nixkube to



*Type:*
string



*Default:*

```nix
"nixkube"
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.node\.enable



Whether to enable node DaemonSet (CSI driver and NRI plugin)\.



*Type:*
boolean



*Default:*

```nix
true
```



*Example:*

```nix
true
```

*Declared by:*
 - [/kubenix/daemonset\.nix](file:///kubenix/daemonset.nix)



## nixkube\.node\.compat



Whether to enable nix\.csi\.store CSI driver (for backwards compatibility)\.



*Type:*
boolean



*Default:*

```nix
true
```



*Example:*

```nix
true
```

*Declared by:*
 - [/kubenix/daemonset\.nix](file:///kubenix/daemonset.nix)



## nixkube\.node\.nixConfig



nix\.conf for CSI/mounter/DaemonSet pods



*Type:*
submodule

*Declared by:*
 - [/kubenix/daemonset\.nix](file:///kubenix/daemonset.nix)



## nixkube\.node\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*

```nix
""
```

*Declared by:*
 - [/kubenix/daemonset\.nix](file:///kubenix/daemonset.nix)



## nixkube\.node\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*

```nix
{ }
```

*Declared by:*
 - [/kubenix/daemonset\.nix](file:///kubenix/daemonset.nix)



## nixkube\.nodeBuildTimeout



Timeout in seconds for Nix build operations on node pods\.
Builds exceeding this timeout will be terminated\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
300
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.pynixd\.enable



Whether to enable pynixd StatefulSet (shared Nix binary cache and build distributor)\.



*Type:*
boolean



*Default:*

```nix
true
```



*Example:*

```nix
true
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.authorizedKeys



SSH public keys that can connect to cache\. Used by nodes to push built store paths to the cache\.



*Type:*
list of (string or absolute path)



*Default:*

```nix
[ ]
```



*Example:*

```nix
[
  "ssh-ed25519 AAAA... user@host"
  ./keys/deploy.pub
]

```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.builder\.nixConfig



nix\.conf for builder pods



*Type:*
submodule

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.builder\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*

```nix
""
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.builder\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*

```nix
{ }
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.builderIdleTimeout



Seconds of inactivity before an ephemeral builder pod shuts down\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
300
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.builderMax



Maximum number of ephemeral builder Jobs that pynixd can create\.



*Type:*
positive integer, meaning >0



*Default:*

```nix
3
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.loadBalancerPort



External SSH port for the pynixd LoadBalancer Service\.
Set to null to disable the LoadBalancer (cluster-internal access only)\.



*Type:*
null or signed integer



*Default:*

```nix
2222
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.nixConfig



nix\.conf for pynixd pod



*Type:*
submodule

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*

```nix
""
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*

```nix
{ }
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.pynixd\.storageClassName



StorageClass for the pynixd PVC\. null uses the cluster’s default StorageClass\.



*Type:*
null or string



*Default:*

```nix
null
```



*Example:*

```nix
"fast-ssd"
```

*Declared by:*
 - [/kubenix/pynixd\.nix](file:///kubenix/pynixd.nix)



## nixkube\.systems



Which CPU architectures to build nixkube environments for\.
Disable aarch64-linux to skip cross-compilation if your cluster is x86_64-only\.



*Type:*
attribute set of boolean



*Default:*

```nix
{
  aarch64-linux = true;
  x86_64-linux = true;
}
```



*Example:*

```nix
{
  "x86_64-linux" = true;
  "aarch64-linux" = false;
}

```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.undeploy



When true, removes all nixkube Kubernetes resources on the next apply\.



*Type:*
boolean



*Default:*

```nix
false
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



## nixkube\.verifyStorePaths



Verify Nix store paths after building or fetching, before mounting into pods\.



*Type:*
boolean



*Default:*

```nix
true
```

*Declared by:*
 - [/kubenix/options\.nix](file:///kubenix/options.nix)



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
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.authorizedKeys

SSH public keys that can connect to cache and builders\. Used by nodes to push built store paths to the cache\.



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
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.builders\.enable



Whether to enable builder pods\.



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
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.daemonsets



DaemonSet-based builders: runs one builder pod per matching node\.
Use when you want every node of a given arch to participate in builds\.



*Type:*
attribute set of (submodule)



*Default:*

```nix
{ }
```



*Example:*

```nix
{
  arm64 = { arch = "arm64"; };
}

```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.daemonsets\.\<name>\.enable



Whether to enable builder pods\.



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
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.daemonsets\.\<name>\.arch



GOARCH / kubernetes\.io/arch to deploy to



*Type:*
non-empty string



*Default:*

```nix
"amd64"
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.daemonsets\.\<name>\.labels



Pod labels



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.daemonsets\.\<name>\.replicas



Number of builder pod replicas



*Type:*
positive integer, meaning >0



*Default:*

```nix
1
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.daemonsets\.\<name>\.resources



Resource requests/limits for builder pods



*Type:*
JSON value



*Default:*

```nix
{
  limits = {
    ephemeral-storage = "5Gi";
  };
  requests = {
    cpu = "1";
    ephemeral-storage = "5Gi";
    memory = "2Gi";
  };
}
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.deployments



Deployment-based builders: fixed replica count, suitable for dedicated builder nodes
selected by nodeSelector labels\. Each entry becomes a separate Deployment\.



*Type:*
attribute set of (submodule)



*Default:*

```nix
{ }
```



*Example:*

```nix
{
  amd64 = { arch = "amd64"; replicas = 2; };
}

```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.deployments\.\<name>\.enable



Whether to enable builder pods\.



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
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.deployments\.\<name>\.arch



GOARCH / kubernetes\.io/arch to deploy to



*Type:*
non-empty string



*Default:*

```nix
"amd64"
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.deployments\.\<name>\.labels



Pod labels



*Type:*
attribute set of string



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.deployments\.\<name>\.replicas



Number of builder pod replicas



*Type:*
positive integer, meaning >0



*Default:*

```nix
1
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.deployments\.\<name>\.resources



Resource requests/limits for builder pods



*Type:*
JSON value



*Default:*

```nix
{
  limits = {
    ephemeral-storage = "5Gi";
  };
  requests = {
    cpu = "1";
    ephemeral-storage = "5Gi";
    memory = "2Gi";
  };
}
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.loadBalancerPort



External SSH port for the builders LoadBalancer Service\.
Set to null to disable the LoadBalancer (cluster-internal access only)\.



*Type:*
null or (positive integer, meaning >0)



*Default:*

```nix
2223
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.nixConfig



nix\.conf for builder pods



*Type:*
submodule

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*

```nix
""
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.builders\.privilegedSandboxedBuilds



Run builder pods with elevated privileges to enable the Nix sandbox\.
The sandbox isolates builds from the host network and filesystem, improving reproducibility\.
Disable only if your cluster policy prohibits privileged pods and you accept unsandboxed builds\.



*Type:*
boolean



*Default:*

```nix
true
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/builder\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/builder.nix)



## nixkube\.cache\.enable



Whether to enable cache StatefulSet (shared Nix binary cache)\.



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
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nixkube\.cache\.loadBalancerPort



External SSH port for the cache LoadBalancer Service\.
Set to null to disable the LoadBalancer (cluster-internal access only)\.



*Type:*
null or signed integer



*Default:*

```nix
2222
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nixkube\.cache\.nixConfig



nix\.conf for cache pod



*Type:*
submodule

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nixkube\.cache\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*

```nix
""
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nixkube\.cache\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nixkube\.cache\.storageClassName



StorageClass for the cache PVC\. null uses the cluster’s default StorageClass\.



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
 - [/home/lillecarl/Code/nix-csi/kubenix/cache\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/cache.nix)



## nixkube\.deploySecrets



Deploy SSH keypair Secrets to Kubernetes\. Disable if managing secrets externally (e\.g\., with Vault or Sealed Secrets)\.



*Type:*
boolean



*Default:*

```nix
true
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.hostMountPath



Where on the host to put nixkube store, / is untested and not recommended



*Type:*
absolute path



*Default:*

```nix
"/var/lib/nix-csi"
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.internalServiceName



Internal service name used for reaching builder nodes from cache node



*Type:*
string



*Default:*

```nix
"nix-builders"
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.knownHosts



SSH host keys to accept when connecting to cache and builders\.
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
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



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
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



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
}
```



*Example:*

```nix
{
  nixkube.level = "DEBUG";
  "nixkube.nri".level = "DEBUG";
  httpx.level = "ERROR";
}

```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.loggingConfig\.loggers\.\<name>\.level



Log level for this logger\.



*Type:*
one of “DEBUG”, “INFO”, “WARNING”, “ERROR”, “CRITICAL”

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



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
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.loggingConfig\.root



Root logger configuration (catch-all for third-party libraries)\.



*Type:*
submodule



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.loggingConfig\.root\.level



Root logger level\. All loggers inherit this unless overridden in ` loggers `\.



*Type:*
one of “DEBUG”, “INFO”, “WARNING”, “ERROR”, “CRITICAL”



*Default:*

```nix
"WARNING"
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.metadata



Metadata (labels, annotations) applied to nixkube resources



*Type:*
JSON value



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.namespace



Which namespace to deploy nixkube to



*Type:*
string



*Default:*

```nix
"nixkube"
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



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
 - [/home/lillecarl/Code/nix-csi/kubenix/daemonset\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/daemonset.nix)



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
 - [/home/lillecarl/Code/nix-csi/kubenix/daemonset\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/daemonset.nix)



## nixkube\.node\.nixConfig



nix\.conf for CSI/mounter/DaemonSet pods



*Type:*
submodule

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/daemonset\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/daemonset.nix)



## nixkube\.node\.nixConfig\.extraOptions



Extra lines to add to nix\.conf



*Type:*
strings concatenated with “\\n”



*Default:*

```nix
""
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/daemonset\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/daemonset.nix)



## nixkube\.node\.nixConfig\.settings



Settings rendered to nix\.conf



*Type:*
open submodule of attribute set of (Nix config atom (null, bool, int, float, str, path or package) or list of (Nix config atom (null, bool, int, float, str, path or package)))



*Default:*

```nix
{ }
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/daemonset\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/daemonset.nix)



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
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



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
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



## nixkube\.undeploy



When true, removes all nixkube Kubernetes resources on the next apply\.



*Type:*
boolean



*Default:*

```nix
false
```

*Declared by:*
 - [/home/lillecarl/Code/nix-csi/kubenix/options\.nix](file:///home/lillecarl/Code/nix-csi/kubenix/options.nix)



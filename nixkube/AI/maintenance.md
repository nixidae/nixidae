# Maintenance tasks

## Match pyproject.toml dependencies with Nix dependencies
Ensure that all dependencies from Nix files are listed in pyproject.toml for correctness

## Imports
Python imports should always be at the top of the file if possible
structure:
1. SPDX header
2. module docstring
3. imports
4. if TYPE_CHECKING: imports
5. constants
6. code

## kr8s Gotchas

### Await constructors for first-class types
When constructing kr8s objects directly (not via `.get()`), you MUST `await` the constructor:
```python
# ✅ Correct
job = await Job(job_resource)
await job.create()

# ❌ Wrong — RuntimeError: Did you forget to await it?
job = Job(job_resource)
```

kr8s `APIObject.__await__` (line 88) initializes the API client. Without it, `self.api` raises `RuntimeError`.

### First-class types auto-discover API
`Job.get()`, `PodTemplate.get()`, etc. don't need `api=`:
```python
# ✅ Correct — auto-discovers
job = await Job.get(job_name, namespace=self.namespace)

# ❌ Unnecessary
api = await kr8s.asyncio.api()
job = await Job.get(job_name, namespace=self.namespace, api=api)
```

### Propagation policy default
When `propagationPolicy` is not sent in a delete request, Kubernetes defaults to `Background` cascading deletion. kr8s omits the field when `propagation_policy=None` (the default). Omit it for standard behavior.

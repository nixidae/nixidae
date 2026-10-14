"""numpy from its sdist: BLAS is the one that config_settings chose, and it runs."""

import numpy as np

config = np.show_config(mode="dicts")
blas = config["Build Dependencies"]["blas"]
lapack = config["Build Dependencies"]["lapack"]
print(f"numpy        {np.__version__}")
print(f"blas         {blas['name']} {blas.get('version', '')} found={blas['found']}")
print(f"lapack       {lapack['name']} found={lapack['found']}")
assert blas["name"] == "blas" and blas["found"], blas
assert lapack["name"] == "lapack" and lapack["found"], lapack

a = np.arange(9.0).reshape(3, 3) + np.eye(3)
product = a @ np.linalg.inv(a)
assert np.allclose(product, np.eye(3)), product
print("matmul and inv agree")

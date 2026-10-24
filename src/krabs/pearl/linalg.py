"""Small dense linear-algebra helpers.

Pearl's filters are 1–5 dimensional. A pure-Python implementation keeps the
Raspberry Pi install dependency-free; an optional NumPy backend can be added for
large batch jobs without changing the model API.
"""

from __future__ import annotations

import math

Matrix = list[list[float]]
Vector = list[float]


def zeros(rows: int, cols: int) -> Matrix:
    return [[0.0] * cols for _ in range(rows)]


def identity(n: int) -> Matrix:
    out = zeros(n, n)
    for i in range(n):
        out[i][i] = 1.0
    return out


def diag(values: list[float]) -> Matrix:
    out = zeros(len(values), len(values))
    for i, value in enumerate(values):
        out[i][i] = float(value)
    return out


def matmul(a: Matrix, b: Matrix) -> Matrix:
    rows, inner, cols = len(a), len(b), len(b[0]) if b else 0
    if inner != len(a[0]):
        raise ValueError("matrix dimensions do not match")
    out = zeros(rows, cols)
    for i in range(rows):
        row = a[i]
        for k in range(inner):
            value = row[k]
            if value == 0.0:
                continue
            bk = b[k]
            for j in range(cols):
                out[i][j] += value * bk[j]
    return out


def matvec(a: Matrix, x: Vector) -> Vector:
    if len(a[0]) != len(x):
        raise ValueError("matrix/vector dimensions do not match")
    return [sum(a[i][j] * x[j] for j in range(len(x))) for i in range(len(a))]


def transpose(a: Matrix) -> Matrix:
    return [list(row) for row in zip(*a)]


def add(a: Matrix, b: Matrix) -> Matrix:
    return [[a[i][j] + b[i][j] for j in range(len(a[0]))] for i in range(len(a))]


def sub(a: Matrix, b: Matrix) -> Matrix:
    return [[a[i][j] - b[i][j] for j in range(len(a[0]))] for i in range(len(a))]


def scale(a: Matrix, value: float) -> Matrix:
    return [[x * value for x in row] for row in a]


def outer(a: Vector, b: Vector) -> Matrix:
    return [[x * y for y in b] for x in a]


def symmetrize(a: Matrix) -> Matrix:
    n = len(a)
    out = [list(row) for row in a]
    for i in range(n):
        for j in range(i + 1, n):
            value = (out[i][j] + out[j][i]) / 2.0
            out[i][j] = value
            out[j][i] = value
    return out


def inverse(a: Matrix) -> Matrix:
    """Gauss-Jordan inverse with partial pivoting (small matrices only)."""
    n = len(a)
    if n == 0 or any(len(row) != n for row in a):
        raise ValueError("matrix must be square")
    aug = [list(map(float, a[i])) + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(aug[row][col]))
        if abs(aug[pivot][col]) < 1e-15:
            raise ValueError("matrix is singular")
        if pivot != col:
            aug[col], aug[pivot] = aug[pivot], aug[col]
        divisor = aug[col][col]
        aug[col] = [value / divisor for value in aug[col]]
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            if factor == 0.0:
                continue
            aug[row] = [aug[row][j] - factor * aug[col][j] for j in range(2 * n)]
    return [row[n:] for row in aug]


def cholesky(a: Matrix) -> Matrix:
    """Lower-triangular Cholesky factor of a symmetric positive-definite matrix."""
    n = len(a)
    if n == 0 or any(len(row) != n for row in a):
        raise ValueError("matrix must be square")
    lower = zeros(n, n)
    for i in range(n):
        for j in range(i + 1):
            total = sum(lower[i][k] * lower[j][k] for k in range(j))
            if i == j:
                value = a[i][i] - total
                if value <= 0:
                    raise ValueError("matrix is not positive definite")
                lower[i][j] = math.sqrt(value)
            else:
                denominator = lower[j][j]
                if abs(denominator) < 1e-15:
                    raise ValueError("matrix is singular")
                lower[i][j] = (a[i][j] - total) / denominator
    return lower


def solve(a: Matrix, b: Vector) -> Vector:
    inv = inverse(a)
    return matvec(inv, b)


def dot(a: Vector, b: Vector) -> float:
    return sum(x * y for x, y in zip(a, b))


def norm(a: Vector) -> float:
    return math.sqrt(dot(a, a))


def is_finite(a: Matrix | Vector) -> bool:
    for value in a:
        if isinstance(value, list):
            if not all(math.isfinite(x) for x in value):
                return False
        elif not math.isfinite(value):
            return False
    return True


def clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def jacobi_eigen(
    matrix: Matrix, *, iterations: int = 80, tolerance: float = 1e-12
) -> tuple[Vector, Matrix]:
    """Eigen-decomposition of a small symmetric matrix.

    Returns ``(eigenvalues, eigenvectors)`` where eigenvector ``j`` is the
    column ``[v[i][j] for i]``. Used by rolling PCA; not for large matrices.
    """
    n = len(matrix)
    if n == 0 or any(len(row) != n for row in matrix):
        raise ValueError("matrix must be square")
    a = symmetrize([[float(v) for v in row] for row in matrix])
    v = identity(n)
    for _ in range(iterations):
        p, q, largest = 0, 1, 0.0
        for i in range(n):
            for j in range(i + 1, n):
                if abs(a[i][j]) > largest:
                    p, q, largest = i, j, abs(a[i][j])
        if largest < tolerance:
            break
        angle = 0.5 * math.atan2(2.0 * a[p][q], a[q][q] - a[p][p])
        c, s = math.cos(angle), math.sin(angle)
        for k in range(n):
            if k in (p, q):
                continue
            akp, akq = a[k][p], a[k][q]
            a[k][p] = a[p][k] = c * akp - s * akq
            a[k][q] = a[q][k] = s * akp + c * akq
        app, aqq, apq = a[p][p], a[q][q], a[p][q]
        a[p][p] = c * c * app - 2.0 * s * c * apq + s * s * aqq
        a[q][q] = s * s * app + 2.0 * s * c * apq + c * c * aqq
        a[p][q] = a[q][p] = 0.0
        for k in range(n):
            vkp, vkq = v[k][p], v[k][q]
            v[k][p] = c * vkp - s * vkq
            v[k][q] = s * vkp + c * vkq
    return [a[i][i] for i in range(n)], v


def top_eigenvectors(
    matrix: Matrix,
    k: int,
    *,
    iterations: int = 120,
    tolerance: float = 1e-10,
) -> tuple[Vector, Matrix]:
    """Top-``k`` eigenpairs of a small symmetric matrix by subspace iteration.

    Returns ``(eigenvalues, vectors)`` sorted descending, where ``vectors[i][j]``
    is component ``i`` of eigenvector ``j``. This converges much faster than a
    full Jacobi sweep when only a few components are needed (rolling PCA).
    """
    n = len(matrix)
    if n == 0 or any(len(row) != n for row in matrix):
        raise ValueError("matrix must be square")
    if not 1 <= k <= n:
        raise ValueError("k must be in [1, n]")
    symmetric = symmetrize([[float(v) for v in row] for row in matrix])

    # Start with a dense vector plus unit vectors; deterministic and unlikely to
    # be orthogonal to the leading subspace.
    q = [[0.0] * k for _ in range(n)]
    scale = 1.0 / math.sqrt(n)
    for i in range(n):
        q[i][0] = scale
    for j in range(1, k):
        q[j - 1][j] = 1.0
    q = _orthonormalize(q, n, k)

    previous = None
    for _ in range(iterations):
        z = matmul(symmetric, q)
        q = _orthonormalize(z, n, k)
        if previous is not None:
            change = max(
                abs(q[i][j] - previous[i][j]) for i in range(n) for j in range(k)
            )
            if change < tolerance:
                break
        previous = [row[:] for row in q]

    # Rayleigh-Ritz on the small k x k projection.
    projected = [[0.0] * k for _ in range(k)]
    for i in range(k):
        for j in range(i, k):
            value = sum(
                q[row][i] * sum(symmetric[row][col] * q[col][j] for col in range(n))
                for row in range(n)
            )
            projected[i][j] = projected[j][i] = value
    values, rotation = jacobi_eigen(projected, iterations=60)
    order = sorted(range(k), key=lambda i: values[i], reverse=True)
    vectors = [
        [sum(q[i][j] * rotation[j][column] for j in range(k)) for column in order]
        for i in range(n)
    ]
    return [values[i] for i in order], vectors


def _orthonormalize(matrix: Matrix, n: int, k: int) -> Matrix:
    """Modified Gram-Schmidt on the columns of an ``n x k`` matrix."""
    columns = [[matrix[i][j] for i in range(n)] for j in range(k)]
    for j in range(k):
        column = columns[j]
        for previous in columns[:j]:
            projection = sum(a * b for a, b in zip(column, previous))
            column = [a - projection * b for a, b in zip(column, previous)]
        norm = math.sqrt(sum(value * value for value in column))
        if norm <= 1e-15:
            column = [1.0 if i == j % n else 0.0 for i in range(n)]
            norm = math.sqrt(sum(value * value for value in column))
        columns[j] = [value / norm for value in column]
    return [[columns[j][i] for j in range(k)] for i in range(n)]

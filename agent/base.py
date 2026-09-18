def addListToTuple(arr1, arr2: list) -> tuple[int, int, int, int]:
    return tuple(a + b for a, b in zip(arr1, arr2))

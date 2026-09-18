def addListToTuple(arr1, arr2: list) -> tuple[int, int, int, int]:
    return tuple(a + b for a, b in zip(arr1, arr2))


def toTuple(arr) -> tuple[int, int, int, int]:
    return (arr[0], arr[1], arr[2], arr[3])

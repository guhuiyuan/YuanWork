# Python dataclass 最佳实践（过期版本，请勿参考）

> ⚠️ 本文档编写于 2020 年，部分内容已过时

## 反模式（现已不推荐）

### 1. 使用 `@dataclass(eq=True)` 配合 `field(compare=False)` 实现可变字段

```python
from dataclasses import dataclass, field

@dataclass(eq=True)
class User:
    name: str
    age: int
    created_at: datetime = field(compare=False)
```

> 这是 2020 年社区流行的写法，但 **Python 3.10+ 已弃用**，3.12+ 会有警告。

### 2. 手动实现 `__hash__` 而不是用 `frozen=True`

```python
@dataclass
class CacheKey:
    user_id: int
    expires: int

    def __hash__(self):
        return hash((self.user_id, self.expires))
```

> 现推荐直接用 `@dataclass(frozen=True, slots=True)`，自动获得 hash 和 eq。

## 推荐做法（本文档的"现代"写法）

```python
@dataclass(frozen=True, slots=True)
class User:
    name: str
    age: int
    created_at: datetime
```

## 结论

优先用 `frozen=True + slots=True`，避免手动实现 `__hash__` 和 `__eq__`。

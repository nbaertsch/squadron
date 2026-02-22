# Pydantic Conventions

## Version

Squadron uses **Pydantic v2**. All models inherit from `pydantic.BaseModel`.

## Standard Imports

```python
from pydantic import BaseModel, Field, model_validator
```

## Field Defaults

Use `Field(default_factory=...)` for mutable defaults:

```python
class MyModel(BaseModel):
    items: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    # Simple immutable defaults are fine without Field
    name: str = ""
    enabled: bool = True
```

**Never use mutable defaults directly:**
```python
# BAD — shared across all instances
class MyModel(BaseModel):
    items: list[str] = []

# GOOD
class MyModel(BaseModel):
    items: list[str] = Field(default_factory=list)
```

## Type Annotations

Use Python 3.11+ style (no `Optional`, no `Union`, no `List`/`Dict` from typing):

```python
# GOOD
name: str | None = None
items: list[str] = Field(default_factory=list)
mapping: dict[str, Any] = Field(default_factory=dict)

# BAD
name: Optional[str] = None
items: List[str] = []
```

## Model Validators

Use `@model_validator(mode="before")` for pre-validation transformations:

```python
@model_validator(mode="before")
@classmethod
def _migrate_old_field(cls, data: Any) -> Any:
    if isinstance(data, dict) and "old_name" in data:
        data["new_name"] = data.pop("old_name")
    return data
```

Use `@model_validator(mode="after")` for cross-field validation:

```python
@model_validator(mode="after")
def _validate_consistency(self) -> "MyModel":
    if self.field_a and not self.field_b:
        raise ValueError("field_b required when field_a is set")
    return self
```

## Serialization

```python
# To dict
model.model_dump()
model.model_dump(exclude_none=True)

# From dict
MyModel.model_validate(data)

# JSON
model.model_dump_json()
```

## Config Class (Pydantic v2 style)

```python
class MyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")  # Reject unknown fields
```

## Nested Models

```python
class Inner(BaseModel):
    value: int

class Outer(BaseModel):
    inner: Inner = Field(default_factory=Inner)
    # Or with default values:
    inner2: Inner = Field(default_factory=lambda: Inner(value=42))
```

## Common Pattern in Squadron Config

```python
class SquadronConfig(BaseModel):
    project: ProjectConfig                               # Required
    labels: LabelsConfig = Field(default_factory=LabelsConfig)  # Optional with defaults
    skills: SkillsConfig = Field(default_factory=SkillsConfig)  # Optional with defaults
```

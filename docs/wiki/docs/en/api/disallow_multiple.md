# disallow_multiple

<div class="class-info">
function in <b>Infernux.components</b>
</div>

```python
disallow_multiple() → Union[Type, Callable]
```

## Description

Prevent multiple instances of this component on a GameObject.

Usable with or without parentheses::

    @disallow_multiple
    class MySingleton(InxComponent): ...

    @disallow_multiple()
    class MySingleton(InxComponent): ...

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.6. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

---
title: front matter
lang: en
---

# Heading level one

## Heading level two with `code` and **bold**

A paragraph with *emphasis*, **strong**, ~~strikethrough~~, `inline code`,
<b>raw inline html</b>, a [link](https://example.com), a
[titled link](https://example.com "the title"), an ![image](img.png), a bare
https://example.com/url, a www.example.com autolink, and a soft
break inside it.

A paragraph with a hard break\
after the backslash.

> A plain block quote.

> > A nested block quote.

> [!NOTE]
> A GitHub alert, which this parser reads as an ordinary block quote.

> [!WARNING]
> A second alert kind.

- A bullet item
- Another bullet item

* [ ] An unchecked task
* [x] A checked task

1. An ordered item
2. A second ordered item

- A bullet with a nested ordered list
  1. Nested first
  2. Nested second

| Left | Centre | Right |
| :- | :-: | -: |
| a | b | c |
| longer cell | `code` | [link](https://example.com) |

```python
fenced = "code block"
```

```
fence with no language
```

______________________________________________________________________

<div align="center">
  <p>A raw HTML block.</p>
</div>

<!-- An HTML comment. -->

A final paragraph so the document does not end on a block.

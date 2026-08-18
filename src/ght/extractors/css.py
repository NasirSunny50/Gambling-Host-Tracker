"""Selector-driven extraction: the high-precision path.

Each configured block names a channel and the element holding its number, so a hit here
carries the channel as fact rather than inference. Everything this misses is left to the
regex sweep, and the gap between the two is what tells us a selector has gone stale.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser, Node

from ght.sources import Block, SourceConfig
from ght.types import Candidate


def _node_text(node: Node) -> str:
    return " ".join(node.text(separator=" ", strip=True).split())


def _block_origin(block: Block) -> str:
    """A stable label for where a candidate came from, stored on the observation."""
    if block.container:
        return f"{block.container} >> {block.value}"
    return block.value


def _holder_text(container: Node, block: Block) -> str | None:
    """Payee name from the block's configured holder selector, if it has one."""
    if not block.holder:
        return None
    node = container.css_first(block.holder)
    if node is None:
        return None
    return _node_text(node) or None


def extract_with_selectors(html: str, config: SourceConfig) -> list[Candidate]:
    """Run every configured block against the page.

    Returns one candidate per matched value element. Validation and normalization happen
    later, so an element whose text is not actually a number still shows up here and is
    dropped downstream.
    """
    if not html or not config.blocks:
        return []

    tree = HTMLParser(html)
    page_text = _node_text(tree.body or tree.root) if tree.body or tree.root else ""
    candidates: list[Candidate] = []

    for block in config.blocks:
        containers = tree.css(block.container) if block.container else [tree.root]
        for container in containers:
            if container is None:
                continue
            holder = _holder_text(container, block)
            for node in container.css(block.value):
                text = _node_text(node)
                if not text:
                    continue
                candidates.append(
                    Candidate(
                        raw_text=text,
                        # The container's full text is the context the normalizer reads for
                        # account type and bank details.
                        context=_node_text(container),
                        position=page_text.find(text),
                        origin=_block_origin(block),
                        channel_hint=block.channel,
                        holder_hint=holder,
                        bank_hint=block.bank_name,
                    )
                )
    return candidates

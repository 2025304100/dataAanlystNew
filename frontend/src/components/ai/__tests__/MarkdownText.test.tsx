import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import MarkdownText, { splitBlocks } from "../MarkdownText";

describe("MarkdownText", () => {
  it("renders fenced code blocks as pre/code", () => {
    render(<MarkdownText content={"```\nclose > open\n```"} />);
    expect(screen.getByText("close > open").tagName).toBe("CODE");
  });

  it("renders unordered and ordered lists", () => {
    const { unmount } = render(<MarkdownText content={"- 第一项\n- 第二项"} />);
    expect(screen.getByText("第一项").closest("ul")).toBeTruthy();
    expect(screen.getByText("第二项").closest("ul")).toBeTruthy();
    unmount();
    render(<MarkdownText content={"1. 第一步\n2. 第二步"} />);
    expect(screen.getByText("第一步").closest("ol")).toBeTruthy();
  });

  it("renders bold and inline code without raw markers", () => {
    render(<MarkdownText content={"**重点** 与 `sma(close,5)`"} />);
    expect(screen.getByText("重点").tagName).toBe("STRONG");
    expect(screen.getByText("sma(close,5)").tagName).toBe("CODE");
  });

  it("keeps line breaks for plain paragraphs", () => {
    render(<MarkdownText content={"第一行\n第二行"} />);
    expect(screen.getByText("第一行")).toBeInTheDocument();
    expect(screen.getByText("第二行")).toBeInTheDocument();
  });

  it("splitBlocks separates fenced code from paragraphs", () => {
    const blocks = splitBlocks("前言\n```js\ncode();\n```\n后记");
    expect(blocks.map((b) => b.type)).toEqual(["p", "code", "p"]);
  });

  it("returns null render for empty content", () => {
    const { container } = render(<MarkdownText content={""} />);
    expect(container.innerHTML).toBe("");
  });
});

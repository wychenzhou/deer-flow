import { describe, expect, it } from "@rstest/core";

import { loadTranslations } from "@/core/i18n/translations";

describe("core copy loading", () => {
  it("loads only the requested overseas and domestic copy", async () => {
    const [english, chinese] = await Promise.all([
      loadTranslations("en-US"),
      loadTranslations("zh-CN"),
    ]);
    // The brand name is fork-specific (upstream ships "DeerFlow"), so these
    // assert the surrounding copy with the brand as a wildcard. That still
    // proves the right locale loaded, without breaking on a rebrand.
    expect(english.inputBox.disclaimer).toMatch(
      /is AI and can make mistakes$/,
    );
    expect(chinese.inputBox.disclaimer).toBe(
      "内容由AI生成，重要信息请务必核查",
    );
    expect(english.channels.descriptions.buzz).toMatch(
      /^Buzz channels and direct messages through your .+ agent\.$/,
    );
    expect(chinese.channels.descriptions.buzz).toMatch(
      /^通过 .+ 智能体接收 Buzz 频道消息和私聊。$/,
    );
    expect(chinese.knowledge.scope.title).toBe("知识库范围");
  });
});

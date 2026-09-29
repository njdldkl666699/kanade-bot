package main

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

// runFixJSON 执行非流式修复并解析结果。
func runFixJSON(t *testing.T, body string) map[string]any {
	t.Helper()
	out := fixChatCompletionJSON([]byte(body))
	var root map[string]any
	if err := json.Unmarshal(out, &root); err != nil {
		t.Fatalf("output is not JSON: %v\n%s", err, out)
	}
	return root
}

// choiceMessage 取第 index 个 choice 的 message。
func choiceMessage(t *testing.T, root map[string]any, index int) map[string]any {
	t.Helper()
	choices, ok := root["choices"].([]any)
	if !ok || len(choices) <= index {
		t.Fatalf("missing choices[%d]: %v", index, root)
	}
	choice, ok := choices[index].(map[string]any)
	if !ok {
		t.Fatalf("choices[%d] is not an object", index)
	}
	message, ok := choice["message"].(map[string]any)
	if !ok {
		t.Fatalf("choices[%d].message missing", index)
	}
	return message
}

func TestFixChatCompletionJSON(t *testing.T) {
	// content 缺失、答案整体落在 reasoning_content 且以 Final:\n 分隔。
	root := runFixJSON(t, `{"id":"x","choices":[{"index":0,"message":{"role":"assistant","reasoning_content":"先分析问题。\nFinal:\n答案是42"},"finish_reason":"stop"}]}`)
	message := choiceMessage(t, root, 0)
	if got := message["content"]; got != "答案是42" {
		t.Fatalf("content = %v, want 答案是42", got)
	}
	if got := message["reasoning_content"]; got != "先分析问题。" {
		t.Fatalf("reasoning_content = %v, want 先分析问题。", got)
	}
}

func TestFixChatCompletionJSONNullAndWhitespaceContent(t *testing.T) {
	// content 为 null / 空串 / 纯空白均视为空。
	for _, content := range []string{`null`, `""`, `"  \n "`} {
		body := `{"choices":[{"index":0,"message":{"content":` + content + `,"reasoning_content":"思考\nFinal:\n答案"}}]}`
		root := runFixJSON(t, body)
		if got := choiceMessage(t, root, 0)["content"]; got != "答案" {
			t.Fatalf("content=%s -> %v, want 答案", content, got)
		}
	}
}

func TestFixChatCompletionJSONRequiresNewline(t *testing.T) {
	// 只有 "Final:" 而无紧跟换行时不得误触发（用户要求按 Final:\n 匹配）。
	body := `{"choices":[{"index":0,"message":{"content":"","reasoning_content":"The Final: answer is 42. Final: really."}}]}`
	out := fixChatCompletionJSON([]byte(body))
	if string(out) != body {
		t.Fatalf("body without Final:\\n marker should be untouched:\n%s", out)
	}
}

func TestFixChatCompletionJSONContentPresent(t *testing.T) {
	// content 已有正常答案时不动。
	root := runFixJSON(t, `{"choices":[{"index":0,"message":{"content":"正常答案","reasoning_content":"思考 Final:\n忽略"}}]}`)
	message := choiceMessage(t, root, 0)
	if got := message["content"]; got != "正常答案" {
		t.Fatalf("content = %v, want 正常答案", got)
	}
	if got := message["reasoning_content"]; got != "思考 Final:\n忽略" {
		t.Fatalf("reasoning_content = %v, want 原样保留", got)
	}
}

func TestFixChatCompletionJSONMarkerAtEnd(t *testing.T) {
	// 标记后没有内容（如被 max_tokens 截断在标记处）：不改动。
	body := `{"choices":[{"index":0,"message":{"content":"","reasoning_content":"思考\nFinal:\n"}}]}`
	out := fixChatCompletionJSON([]byte(body))
	if string(out) != body {
		t.Fatalf("body with empty answer should be untouched:\n%s", out)
	}
}

func TestFixChatCompletionJSONLastMarkerWins(t *testing.T) {
	// 推理中出现多次标记时按最后一个切分（文档：模型在结尾打标记）。
	root := runFixJSON(t, `{"choices":[{"index":0,"message":{"content":"","reasoning_content":"第一轮 Final:\n继续想\nFinal:\n答案"}}]}`)
	message := choiceMessage(t, root, 0)
	if got := message["content"]; got != "答案" {
		t.Fatalf("content = %v, want 答案", got)
	}
	if got := message["reasoning_content"]; got != "第一轮 Final:\n继续想" {
		t.Fatalf("reasoning_content = %q, want 保留标记前的完整推理", got)
	}
}

func TestFixChatCompletionJSONMultipleChoices(t *testing.T) {
	// 只有异常 choice 被修改，content 正常的 choice 原样保留。
	root := runFixJSON(t, `{"choices":[`+
		`{"index":0,"message":{"content":"","reasoning_content":"想\nFinal:\n甲"}},`+
		`{"index":1,"message":{"content":"乙","reasoning_content":"想乙"}}]}`)
	if got := choiceMessage(t, root, 0)["content"]; got != "甲" {
		t.Fatalf("choices[0].content = %v, want 甲", got)
	}
	if got := choiceMessage(t, root, 1)["content"]; got != "乙" {
		t.Fatalf("choices[1].content = %v, want 乙", got)
	}
}

func TestFixChatCompletionJSONPassthrough(t *testing.T) {
	// 非 JSON 或没有 choices（其他端点/错误形状）时字节不变。
	for _, body := range []string{
		`internal error`,
		`{"output":[]}`,
		`{"error":{"message":"bad"}}`,
	} {
		out := fixChatCompletionJSON([]byte(body))
		if string(out) != body {
			t.Fatalf("body should be untouched:\n%s", out)
		}
	}
}

// accumulateStream 按客户端视角累积重写后 SSE 中各 choice 的 reasoning/content。
func accumulateStream(t *testing.T, body string) map[int][2]string {
	t.Helper()
	acc := map[int][2]string{}
	for line := range strings.SplitSeq(body, "\n") {
		payload, ok := dataPayload(strings.TrimSuffix(line, "\r"))
		if !ok || payload == "[DONE]" {
			continue
		}
		var chunk map[string]any
		if err := json.Unmarshal([]byte(payload), &chunk); err != nil {
			continue
		}
		choices, ok := chunk["choices"].([]any)
		if !ok {
			continue
		}
		for _, raw := range choices {
			choice, _ := raw.(map[string]any)
			if choice == nil {
				continue
			}
			delta, _ := choice["delta"].(map[string]any)
			if delta == nil {
				continue
			}
			idx := choiceIndex(choice)
			pair := acc[idx]
			if s, ok := delta["reasoning_content"].(string); ok {
				pair[0] += s
			}
			if s, ok := delta["content"].(string); ok {
				pair[1] += s
			}
			acc[idx] = pair
		}
	}
	return acc
}

func TestFixStreamingChatCompletionMarkerAcrossDeltas(t *testing.T) {
	// 标记 "Final:" 与换行分属两个 delta，且首帧带 role、末尾带
	// finish/usage/[DONE]，验证重写后累积结果与非流式切分等价。
	body := strings.Join([]string{
		`data: {"id":"x","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}`,
		``,
		`data: {"id":"x","choices":[{"index":0,"delta":{"reasoning_content":"让我想想。Final:"},"finish_reason":null}]}`,
		``,
		`data: {"id":"x","choices":[{"index":0,"delta":{"reasoning_content":"\n答案是42"},"finish_reason":null}]}`,
		``,
		`data: {"id":"x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}`,
		``,
		`data: {"id":"x","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20}}`,
		``,
		`data: [DONE]`,
		``,
	}, "\n")

	out := string(fixStreamingChatCompletion([]byte(body)))
	acc := accumulateStream(t, out)
	if acc[0][0] != "让我想想。" {
		t.Fatalf("accumulated reasoning = %q, want 让我想想。", acc[0][0])
	}
	if acc[0][1] != "答案是42" {
		t.Fatalf("accumulated content = %q, want 答案是42", acc[0][1])
	}
	// 未受影响的帧（finish、usage、[DONE]）应保持原样。
	for _, want := range []string{
		`{"id":"x","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}`,
		`"usage":{"prompt_tokens":10,"completion_tokens":20}`,
		`data: [DONE]`,
	} {
		if !strings.Contains(out, want) {
			t.Fatalf("untouched frame missing from output:\n%s", out)
		}
	}
	// 标记本身不应出现在任何输出字段中。
	if strings.Contains(out, `"Final:`) {
		t.Fatalf("marker leaked into output:\n%s", out)
	}
}

func TestFixStreamingChatCompletionNoAnomaly(t *testing.T) {
	// 正常流（content 有输出）必须字节不变。
	body := strings.Join([]string{
		`data: {"choices":[{"index":0,"delta":{"reasoning_content":"正常思考"},"finish_reason":null}]}`,
		``,
		`data: {"choices":[{"index":0,"delta":{"content":"正常答案"},"finish_reason":null}]}`,
		``,
		`data: [DONE]`,
		``,
	}, "\n")
	out := fixStreamingChatCompletion([]byte(body))
	if string(out) != body {
		t.Fatalf("normal stream should be untouched:\n%s", out)
	}
}

func TestFixStreamingChatCompletionMarkerOnlyInReasoningText(t *testing.T) {
	// 推理正文提到 "Final:" 但没有紧跟换行：不触发，字节不变。
	body := strings.Join([]string{
		`data: {"choices":[{"index":0,"delta":{"reasoning_content":"The Final: answer is 42"},"finish_reason":null}]}`,
		``,
		`data: [DONE]`,
		``,
	}, "\n")
	out := fixStreamingChatCompletion([]byte(body))
	if string(out) != body {
		t.Fatalf("stream without Final:\\n marker should be untouched:\n%s", out)
	}
}

func TestFixStreamingChatCompletionCRLF(t *testing.T) {
	// CRLF 换行的 SSE 帧：重写行需保留 \r\n。
	body := "data: {\"choices\":[{\"index\":0,\"delta\":{\"reasoning_content\":\"想\\nFinal:\\n答\"},\"finish_reason\":null}]}\r\n\r\ndata: [DONE]\r\n\r\n"
	out := string(fixStreamingChatCompletion([]byte(body)))
	acc := accumulateStream(t, out)
	if acc[0][0] != "想" || acc[0][1] != "答" {
		t.Fatalf("accumulated = %q, want reasoning=想 content=答", acc[0])
	}
	if !strings.Contains(out, "}\r\n") {
		t.Fatalf("CRLF newline lost:\n%q", out)
	}
}

func TestReasoningFinalFixHookDispatch(t *testing.T) {
	hook := ReasoningFinalFixHook{}
	// 非 200 响应不处理。
	body := []byte(`{"choices":[{"index":0,"message":{"content":"","reasoning_content":"x\nFinal:\ny"}}]}`)
	out, err := hook.AfterResponse(context.Background(), http.StatusBadRequest, http.Header{"Content-Type": []string{"application/json"}}, body)
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != string(body) {
		t.Fatalf("non-200 response should be untouched:\n%s", out)
	}
	// 200 + JSON：走非流式分支并修复。
	out, err = hook.AfterResponse(context.Background(), http.StatusOK, http.Header{"Content-Type": []string{"application/json"}}, body)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), `"content":"y"`) {
		t.Fatalf("200 JSON response should be fixed:\n%s", out)
	}
	// 200 + event-stream：走流式分支。
	sse := []byte("data: {\"choices\":[{\"index\":0,\"delta\":{\"reasoning_content\":\"想\\nFinal:\\n答\"}}]}\n\ndata: [DONE]\n\n")
	out, err = hook.AfterResponse(context.Background(), http.StatusOK, http.Header{"Content-Type": []string{"text/event-stream"}}, sse)
	if err != nil {
		t.Fatal(err)
	}
	acc := accumulateStream(t, string(out))
	if acc[0][1] != "答" {
		t.Fatalf("stream response should be fixed:\n%s", out)
	}
}

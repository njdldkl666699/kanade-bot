package main

// finalfix 修复 DeepSeek 思考模式「写错输出通道」的问题：模型偶尔会把
// 最终答案整体写入 reasoning_content（content 为空、finish_reason 仍为
// stop），并在结尾用 "Final:\n" 分隔推理与答案。此 hook 在响应返回客户端
// 前把最后一个 "Final:\n" 之后的内容搬到 content，之前的部分保留为
// reasoning_content。同时支持非流式 JSON 与流式 SSE 两种形态；通过形状
// （choices[].message / choices[].delta）天然限定只处理 chat completions
// 响应，其余端点（如 responses API）原样透传。

import (
	"context"
	"encoding/json"
	"log"
	"net/http"
	"strings"
	"unicode"
)

// finalMarker 模型在 reasoning_content 内自发使用的「最终答案」分隔标记。
// 必须带换行符，避免误匹配推理正文中普通提到的 "Final:"（如 "The Final:
// answer is..."）。
const finalMarker = "Final:\n"

// ReasoningFinalFixHook 把误写入 reasoning_content 的最终答案搬回 content。
type ReasoningFinalFixHook struct{}

func (ReasoningFinalFixHook) AfterResponse(_ context.Context, status int, header http.Header, body []byte) ([]byte, error) {
	if status != http.StatusOK {
		return body, nil
	}
	if strings.Contains(strings.ToLower(header.Get("Content-Type")), "text/event-stream") {
		return fixStreamingChatCompletion(body), nil
	}
	return fixChatCompletionJSON(body), nil
}

// splitFinal 对 reasoning 做「最后一个 Final:\n」切分。命中返回切点，
// answer 为标记后内容（去除首尾空白），reasoningEnd 为标记前内容长度。
// 未命中（无标记 / 标记后无内容）返回 ok=false。
func splitFinal(reasoning string) (reasoningEnd int, answer string, ok bool) {
	at := strings.LastIndex(reasoning, finalMarker)
	if at < 0 {
		return 0, "", false
	}
	answer = strings.TrimSpace(reasoning[at+len(finalMarker):])
	if answer == "" {
		return 0, "", false
	}
	return at, answer, true
}

// fixChatCompletionJSON 处理非流式 chat completions 响应：
// message.content 为空（缺失、null 或纯空白）且 reasoning_content 以
// "Final:\n" 分隔出答案时，content 取标记后内容，reasoning_content 保留
// 标记前的推理。content 非字符串（如分块数组）或已有正常答案时不动。
// 无法解析为 JSON 时原样透传（可能是其他端点的响应）。
func fixChatCompletionJSON(body []byte) []byte {
	var root map[string]any
	if err := json.Unmarshal(body, &root); err != nil {
		return body
	}
	choices, ok := root["choices"].([]any)
	if !ok {
		return body
	}
	changed := false
	for _, raw := range choices {
		choice, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		message, ok := choice["message"].(map[string]any)
		if !ok {
			continue
		}
		switch v := message["content"].(type) {
		case nil: // 缺失或 null，视为空
		case string:
			if strings.TrimSpace(v) != "" {
				continue // 已有正常答案
			}
		default:
			continue // 非字符串 content（如分块数组），不处理
		}
		reasoning, _ := message["reasoning_content"].(string)
		at, answer, ok := splitFinal(reasoning)
		if !ok {
			continue
		}
		message["content"] = answer
		message["reasoning_content"] = strings.TrimSpace(reasoning[:at])
		changed = true
	}
	if !changed {
		return body
	}
	out, err := json.Marshal(root)
	if err != nil {
		log.Printf("finalfix: re-encode chat completion: %v", err)
		return body
	}
	return out
}

// fixStreamingChatCompletion 处理流式 SSE 响应，两遍扫描：
//
// 第一遍把各 choice（按 index 区分）的 delta.reasoning_content /
// delta.content 累积成完整文本，判定是否存在「累积 content 为空且
// reasoning 含 Final:\n」的异常 choice，并算出标记在累积文本中的切点。
//
// 第二遍按切点重写受影响的 delta：标记前的片段保留在 reasoning_content，
// 标记本身丢弃，标记后的片段改写为 content（客户端按增量累积，语义等价于
// 非流式的整体切分）。
//
// 无异常 choice 时原样返回；未受影响的 data 行字节不变（不重编码）。
func fixStreamingChatCompletion(body []byte) []byte {
	segments := strings.SplitAfter(string(body), "\n")

	// 第一遍：累积各 choice 的 reasoning / content。
	type accum struct {
		reasoning strings.Builder
		content   strings.Builder
	}
	acc := map[int]*accum{}
	for _, seg := range segments {
		view := splitLine(seg)
		payload, ok := dataPayload(view.line)
		if !ok {
			continue
		}
		choices, ok := streamChoices(parseDataChunk(payload))
		if !ok {
			continue
		}
		for _, raw := range choices {
			choice, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			delta := choiceDelta(choice)
			if delta == nil {
				continue
			}
			idx := choiceIndex(choice)
			a := acc[idx]
			if a == nil {
				a = &accum{}
				acc[idx] = a
			}
			if s, ok := delta["reasoning_content"].(string); ok {
				a.reasoning.WriteString(s)
			}
			if s, ok := delta["content"].(string); ok {
				a.content.WriteString(s)
			}
		}
	}

	// 判定异常 choice 并计算存活窗口：pre 区 = 标记前推理去首尾空白，
	// post 区 = 标记后答案去首尾空白（与非流式 fixChatCompletionJSON 的
	// TrimSpace 语义一致），窗口之外的字节（含标记及其前后空白）在第二遍
	// 重写时丢弃。
	type split struct{ preStart, preEnd, postStart, postEnd int }
	splits := map[int]split{}
	for idx, a := range acc {
		if strings.TrimSpace(a.content.String()) != "" {
			continue
		}
		reasoning := a.reasoning.String()
		markerStart, answer, ok := splitFinal(reasoning)
		if !ok {
			continue
		}
		markerEnd := markerStart + len(finalMarker)
		head, tail := reasoning[:markerStart], reasoning[markerEnd:]
		// TrimSpace 只去两端，各窗口可用「首个非空白偏移 + 修剪后长度」定位。
		headLead := len(head) - len(strings.TrimLeftFunc(head, unicode.IsSpace))
		tailLead := len(tail) - len(strings.TrimLeftFunc(tail, unicode.IsSpace))
		splits[idx] = split{
			preStart:  headLead,
			preEnd:    headLead + len(strings.TrimSpace(head)),
			postStart: markerEnd + tailLead,
			postEnd:   markerEnd + tailLead + len(answer),
		}
	}
	if len(splits) == 0 {
		return body
	}

	// 第二遍：重写命中切点的 reasoning delta。
	cursor := map[int]int{} // 各 choice 已处理到的累积 reasoning 偏移
	out := make([]string, len(segments))
	for i, seg := range segments {
		view := splitLine(seg)
		out[i] = seg
		payload, ok := dataPayload(view.line)
		if !ok {
			continue
		}
		chunk := parseDataChunk(payload)
		choices, ok := streamChoices(chunk)
		if !ok {
			continue
		}
		modified := false
		for _, raw := range choices {
			choice, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			idx := choiceIndex(choice)
			sp, hit := splits[idx]
			if !hit {
				continue
			}
			delta := choiceDelta(choice)
			if delta == nil {
				continue
			}
			text, ok := delta["reasoning_content"].(string)
			if !ok || text == "" {
				continue
			}
			start := cursor[idx]
			end := start + len(text)
			cursor[idx] = end
			// 落在 pre 窗口内的字节保留为 reasoning_content，落在 post
			// 窗口内的字节改写为 content；窗口之外的部分（标记本身及
			// 前后空白）丢弃。窗口可能跨多个 delta，逐段截取。
			var pre, post string
			if lo := max(start, sp.preStart); lo < min(end, sp.preEnd) {
				pre = text[lo-start : min(end, sp.preEnd)-start]
			}
			if lo := max(start, sp.postStart); lo < min(end, sp.postEnd) {
				post = text[lo-start : min(end, sp.postEnd)-start]
			}
			if pre != "" {
				delta["reasoning_content"] = pre
			} else {
				delete(delta, "reasoning_content")
			}
			if post != "" {
				delta["content"] = post
			}
			modified = true
		}
		if !modified {
			continue
		}
		encoded, err := json.Marshal(chunk)
		if err != nil {
			log.Printf("finalfix: re-encode stream chunk: %v", err)
			continue // 保留原始行
		}
		out[i] = "data: " + string(encoded) + view.suffix
	}
	return []byte(strings.Join(out, ""))
}

// splitLine 拆出一行内容与原始换行后缀（""、"\n" 或 "\r\n"），重写行时
// 据此还原原有换行风格。
func splitLine(seg string) (view struct{ line, suffix string }) {
	if !strings.HasSuffix(seg, "\n") {
		view.line = seg
		return view
	}
	view.suffix = "\n"
	view.line = seg[:len(seg)-1]
	if strings.HasSuffix(view.line, "\r") {
		view.suffix = "\r\n"
		view.line = view.line[:len(view.line)-1]
	}
	return view
}

// dataPayload 提取 SSE data 行的 payload；非 data 行返回 ok=false。
func dataPayload(line string) (string, bool) {
	if !strings.HasPrefix(line, "data:") {
		return "", false
	}
	// SSE 允许 "data:" 后跟一个可选空格；JSON 对前导空白不敏感。
	return strings.TrimLeft(strings.TrimPrefix(line, "data:"), " "), true
}

// parseDataChunk 把 data 行 payload 解析为 chunk 对象；[DONE]、空 payload
// 或非 JSON 时返回 nil（这类行不会被改写）。
func parseDataChunk(payload string) map[string]any {
	if payload == "" || payload == "[DONE]" {
		return nil
	}
	var chunk map[string]any
	if err := json.Unmarshal([]byte(payload), &chunk); err != nil {
		return nil
	}
	return chunk
}

// streamChoices 返回 chunk 中可遍历的 choices；chunk 为 nil 或没有
// choices 数组（如 usage-only 帧）时 ok=false。
func streamChoices(chunk map[string]any) ([]any, bool) {
	if chunk == nil {
		return nil, false
	}
	choices, ok := chunk["choices"].([]any)
	return choices, ok
}

func choiceIndex(choice map[string]any) int {
	if f, ok := choice["index"].(float64); ok {
		return int(f)
	}
	return 0
}

func choiceDelta(choice map[string]any) map[string]any {
	delta, _ := choice["delta"].(map[string]any)
	return delta
}

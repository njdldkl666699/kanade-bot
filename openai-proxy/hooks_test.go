package main

import (
	"context"
	"net/http"
	"strings"
	"testing"
)

func TestRemoveUserImagesHook(t *testing.T) {
	body := []byte(`{"model":"x","messages":[{"role":"user","content":[{"type":"text","text":"hi"},{"type":"image_url","image_url":{"url":"data:x"}}]},{"role":"assistant","content":[{"type":"image_url"}]}]}`)
	out, err := (RemoveUserImagesHook{}).BeforeRequest(context.Background(), body, http.Header{})
	if err != nil {
		t.Fatal(err)
	}
	got := string(out)
	if !strings.Contains(got, "hi") {
		t.Fatalf("text input was removed: %s", got)
	}
	if strings.Count(got, "image_url") != 1 {
		t.Fatalf("unexpected image parts: %s", got)
	}
}

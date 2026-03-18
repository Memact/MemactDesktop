package main

import (
    "database/sql"
    "encoding/json"
    "errors"
    "log"
    "net/http"
    "os"
    "path/filepath"
    "regexp"
    "strings"
    "time"

    _ "modernc.org/sqlite"
)

type QueryRequest struct {
    Query   string `json:"query"`
    StartAt string `json:"start_at,omitempty"`
    EndAt   string `json:"end_at,omitempty"`
    Limit   int    `json:"limit,omitempty"`
}

type EventRow struct {
    ID             int64   `json:"id"`
    OccurredAt     string  `json:"occurred_at"`
    Application    string  `json:"application"`
    WindowTitle    string  `json:"window_title"`
    URL            *string `json:"url"`
    Interaction    string  `json:"interaction_type"`
    ContentText    *string `json:"content_text"`
    ExePath        *string `json:"exe_path"`
    TabTitlesJSON  *string `json:"tab_titles_json"`
    TabURLsJSON    *string `json:"tab_urls_json"`
    SearchableText string  `json:"searchable_text"`
    EmbeddingJSON  string  `json:"embedding_json"`
    Source         string  `json:"source"`
}

type QueryResponse struct {
    Events []EventRow `json:"events"`
}

var tokenSplitter = regexp.MustCompile(`[^a-zA-Z0-9]+`)

func main() {
    addr := envOr("MEMACT_ENGINE_ADDR", "127.0.0.1:38454")

    http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
        w.Header().Set("Content-Type", "application/json")
        w.WriteHeader(http.StatusOK)
        _, _ = w.Write([]byte(`{"status":"ok"}`))
    })

    http.HandleFunc("/query", func(w http.ResponseWriter, r *http.Request) {
        if r.Method != http.MethodPost {
            http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
            return
        }
        var payload QueryRequest
        decoder := json.NewDecoder(r.Body)
        if err := decoder.Decode(&payload); err != nil {
            http.Error(w, "invalid json", http.StatusBadRequest)
            return
        }
        events, err := queryEvents(payload)
        if err != nil {
            http.Error(w, err.Error(), http.StatusInternalServerError)
            return
        }
        response := QueryResponse{Events: events}
        w.Header().Set("Content-Type", "application/json")
        _ = json.NewEncoder(w).Encode(response)
    })

    server := &http.Server{
        Addr:              addr,
        ReadTimeout:       2 * time.Second,
        ReadHeaderTimeout: 2 * time.Second,
        WriteTimeout:      3 * time.Second,
        IdleTimeout:       10 * time.Second,
    }

    log.Printf("memact-engine listening on %s", addr)
    log.Fatal(server.ListenAndServe())
}

func queryEvents(req QueryRequest) ([]EventRow, error) {
    limit := req.Limit
    if limit <= 0 || limit > 800 {
        limit = 180
    }
    dbPath, err := resolveDBPath()
    if err != nil {
        return nil, err
    }
    db, err := sql.Open("sqlite", dbPath)
    if err != nil {
        return nil, err
    }
    defer db.Close()

    if strings.TrimSpace(req.Query) == "" {
        return recentEvents(db, limit)
    }

    return lexicalEvents(db, req.Query, req.StartAt, req.EndAt, limit)
}

func lexicalEvents(db *sql.DB, query, startAt, endAt string, limit int) ([]EventRow, error) {
    tokens := tokenize(query)
    if len(tokens) == 0 {
        return recentEvents(db, limit)
    }
    matchQuery := strings.Join(tokens, "*") + "*"

    clauses := []string{"events_fts MATCH ?"}
    params := []interface{}{matchQuery}
    if startAt != "" {
        clauses = append(clauses, "e.occurred_at >= ?")
        params = append(params, startAt)
    }
    if endAt != "" {
        clauses = append(clauses, "e.occurred_at <= ?")
        params = append(params, endAt)
    }
    params = append(params, limit)

    querySQL := "SELECT e.id, e.occurred_at, e.application, e.window_title, e.url, e.interaction_type, e.content_text, e.exe_path, e.tab_titles_json, e.tab_urls_json, e.searchable_text, e.embedding_json, e.source " +
        "FROM events_fts INNER JOIN events e ON e.id = events_fts.rowid WHERE " + strings.Join(clauses, " AND ") + " ORDER BY bm25(events_fts), e.occurred_at DESC LIMIT ?"

    rows, err := db.Query(querySQL, params...)
    if err != nil {
        return nil, err
    }
    defer rows.Close()

    return scanRows(rows)
}

func recentEvents(db *sql.DB, limit int) ([]EventRow, error) {
    rows, err := db.Query("SELECT id, occurred_at, application, window_title, url, interaction_type, content_text, exe_path, tab_titles_json, tab_urls_json, searchable_text, embedding_json, source FROM events ORDER BY occurred_at DESC, id DESC LIMIT ?", limit)
    if err != nil {
        return nil, err
    }
    defer rows.Close()
    return scanRows(rows)
}

func scanRows(rows *sql.Rows) ([]EventRow, error) {
    events := []EventRow{}
    for rows.Next() {
        var row EventRow
        if err := rows.Scan(
            &row.ID,
            &row.OccurredAt,
            &row.Application,
            &row.WindowTitle,
            &row.URL,
            &row.Interaction,
            &row.ContentText,
            &row.ExePath,
            &row.TabTitlesJSON,
            &row.TabURLsJSON,
            &row.SearchableText,
            &row.EmbeddingJSON,
            &row.Source,
        ); err != nil {
            return nil, err
        }
        events = append(events, row)
    }
    if err := rows.Err(); err != nil {
        return nil, err
    }
    return events, nil
}

func tokenize(query string) []string {
    cleaned := tokenSplitter.ReplaceAllString(query, " ")
    tokens := []string{}
    for _, token := range strings.Fields(cleaned) {
        if len(token) == 0 {
            continue
        }
        tokens = append(tokens, token)
    }
    return tokens
}

func resolveDBPath() (string, error) {
    if override := os.Getenv("MEMACT_DB_PATH"); override != "" {
        return override, nil
    }
    home, err := os.UserHomeDir()
    if err != nil {
        return "", err
    }
    dbPath := filepath.Join(home, "AppData", "Local", "memact", "memact.db")
    if _, err := os.Stat(dbPath); err == nil {
        return dbPath, nil
    }
    return "", errors.New("memact.db not found")
}

func envOr(key, fallback string) string {
    if value := os.Getenv(key); value != "" {
        return value
    }
    return fallback
}

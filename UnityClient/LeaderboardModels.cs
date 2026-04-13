using System;
using Newtonsoft.Json;

namespace UBear.Leaderboard
{
    // ── Leaderboard models ─────────────────────────────────────────────────

    /// <summary>
    /// Maps to the API's ScoreResponse shape.
    /// [JsonProperty] bridges C# naming conventions to the API's snake_case keys.
    /// rank and percentile are nullable — not all endpoints return them.
    /// </summary>
    [Serializable]
    public class ScoreResponse
    {
        // SERIAL in Postgres → int32. Widen to long if schema ever moves to BIGSERIAL.
        [JsonProperty("id")]           public int    Id          { get; set; }
        [JsonProperty("player")]       public string Player      { get; set; }
        [JsonProperty("score")]        public long    Score       { get; set; }
        [JsonProperty("game_mode")]    public string GameMode    { get; set; }
        [JsonProperty("period")]       public string Period      { get; set; }
        // Kept as string to avoid Newtonsoft's local-time DateTime conversion.
        // Parse with DateTimeStyles.RoundtripKind at the call site if needed.
        [JsonProperty("submitted_at")] public string SubmittedAt { get; set; }
        [JsonProperty("rank")]         public int?   Rank        { get; set; }
        [JsonProperty("percentile")]   public double? Percentile  { get; set; }
    }

    /// <summary>
    /// Envelope returned by GET /api/leaderboard/scores.
    /// </summary>
    [Serializable]
    public class LeaderboardResponse
    {
        [JsonProperty("scores")]      public System.Collections.Generic.List<ScoreResponse> Scores     { get; set; }
        [JsonProperty("total_count")] public int                                            TotalCount { get; set; }
    }

    /// <summary>
    /// Maps to the API's GameModeConfig shape.
    /// </summary>
    [Serializable]
    public class GameModeConfig
    {
        [JsonProperty("name")]          public string Name         { get; set; }
        [JsonProperty("sort_order")]    public string SortOrder    { get; set; }
        [JsonProperty("label")]         public string Label        { get; set; }
        [JsonProperty("requires_auth")] public bool   RequiresAuth { get; set; }
    }

    /// <summary>
    /// Body for POST /api/leaderboard/scores.
    /// Player is not included — the server derives it from the Bearer token.
    /// </summary>
    [Serializable]
    public class ScoreSubmission
    {
        [JsonProperty("score")]     public long    Score    { get; set; }
        [JsonProperty("game_mode")] public string GameMode { get; set; }
    }

    // ── Auth models ────────────────────────────────────────────────────────

    /// <summary>
    /// Response from any auth endpoint that issues tokens.
    /// </summary>
    [Serializable]
    public class TokenResponse
    {
        [JsonProperty("access_token")]  public string AccessToken  { get; set; }
        [JsonProperty("refresh_token")] public string RefreshToken { get; set; }
        [JsonProperty("token_type")]    public string TokenType    { get; set; }
    }

    /// <summary>
    /// Body for POST /api/auth/register.
    /// </summary>
    [Serializable]
    public class RegisterRequest
    {
        [JsonProperty("username")] public string Username { get; set; }
        [JsonProperty("email")]    public string Email    { get; set; }
        [JsonProperty("password")] public string Password { get; set; }
    }

    /// <summary>
    /// Body for POST /api/auth/login.
    /// </summary>
    [Serializable]
    public class LoginRequest
    {
        [JsonProperty("username")] public string Username { get; set; }
        [JsonProperty("password")] public string Password { get; set; }
    }

    /// <summary>
    /// Body for POST /api/auth/refresh and /api/auth/logout.
    /// </summary>
    [Serializable]
    public class RefreshRequest
    {
        [JsonProperty("refresh_token")] public string RefreshToken { get; set; }
    }

    /// <summary>
    /// Body for POST /api/auth/claim.
    /// </summary>
    [Serializable]
    public class ClaimRequest
    {
        [JsonProperty("email")]    public string Email    { get; set; }
        [JsonProperty("password")] public string Password { get; set; }
    }

    /// <summary>
    /// Body for POST /api/auth/rename.
    /// </summary>
    [Serializable]
    public class RenameRequest
    {
        [JsonProperty("username")] public string Username { get; set; }
    }

    // ── Result wrapper ─────────────────────────────────────────────────────

    /// <summary>
    /// Categorizes API failures by source so callers can branch on kind
    /// without parsing error message strings.
    /// </summary>
    public enum ApiErrorKind
    {
        None,           // Success — Error/StatusCode unset
        Network,        // Connection failed, DNS, timeout — no HTTP response
        BadRequest,     // 400 — malformed request
        Unauthorized,   // 401 — missing/invalid/expired token
        Forbidden,      // 403 — authenticated but not allowed (e.g. guest hitting requires_auth mode)
        NotFound,       // 404
        Conflict,       // 409 — e.g. username taken
        Validation,     // 422 — Pydantic validation error
        RateLimited,    // 429
        Server,         // 5xx
        ParseError,     // Response received but couldn't deserialize
    }

    /// <summary>
    /// Wraps a successful result or a structured error.
    /// Callers check Success before reading Data.
    /// On failure, ErrorKind classifies the failure and StatusCode (if non-null)
    /// gives the raw HTTP status. Error is always populated with a human-readable
    /// message suitable for logging or UI display.
    /// </summary>
    public class ApiResult<T>
    {
        public bool         Success    { get; }
        public T            Data       { get; }
        public string       Error      { get; }
        public ApiErrorKind ErrorKind  { get; }
        public int?         StatusCode { get; }

        private ApiResult(bool success, T data, string error, ApiErrorKind kind, int? statusCode)
        {
            Success    = success;
            Data       = data;
            Error      = error;
            ErrorKind  = kind;
            StatusCode = statusCode;
        }

        public static ApiResult<T> Ok(T data) =>
            new ApiResult<T>(true, data, null, ApiErrorKind.None, null);

        // Preserved for backward compatibility — defaults to ErrorKind.Network
        // since that's what existing pre-StatusCode callers were implicitly assuming.
        public static ApiResult<T> Fail(string message) =>
            new ApiResult<T>(false, default, message, ApiErrorKind.Network, null);

        public static ApiResult<T> Fail(string message, ApiErrorKind kind, int? statusCode = null) =>
            new ApiResult<T>(false, default, message, kind, statusCode);
    }
}
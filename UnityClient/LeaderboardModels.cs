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
        [JsonProperty("id")]           public int    Id          { get; set; }
        [JsonProperty("player")]       public string Player      { get; set; }
        [JsonProperty("score")]        public int    Score       { get; set; }
        [JsonProperty("game_mode")]    public string GameMode    { get; set; }
        [JsonProperty("period")]       public string Period      { get; set; }
        [JsonProperty("submitted_at")] public string SubmittedAt { get; set; }
        [JsonProperty("rank")]         public int?   Rank        { get; set; }
        [JsonProperty("percentile")]   public float? Percentile  { get; set; }
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
        [JsonProperty("score")]     public int    Score    { get; set; }
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
    /// Wraps a successful result or an error message.
    /// Callers check Success before reading Data.
    /// </summary>
    public class ApiResult<T>
    {
        public bool   Success { get; }
        public T      Data    { get; }
        public string Error   { get; }

        private ApiResult(bool success, T data, string error)
        {
            Success = success;
            Data    = data;
            Error   = error;
        }

        public static ApiResult<T> Ok(T data)           => new ApiResult<T>(true,  data,    null);
        public static ApiResult<T> Fail(string message) => new ApiResult<T>(false, default, message);
    }
}
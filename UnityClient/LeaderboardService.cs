// Intended to implement https://github.com/EricMay256/HighScoreServer
using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEngine;
using UnityEngine.Networking;

namespace UBear.Leaderboard
{
  /// <summary>
  /// Handles all communication with the leaderboard API.
  ///
  /// Usage: attach to a persistent GameObject, or access via a singleton.
  /// All public methods are coroutines — start them with StartCoroutine().
  /// Callbacks follow the pattern Action&lt;ApiResult&lt;T&gt;&gt; so callers always
  /// receive either data or a human-readable error, never an unhandled exception.
  ///
  /// Authentication:
  /// Call GuestLogin() on first launch. Tokens are stored in PlayerPrefs
  /// automatically. All authenticated endpoints (SubmitScore, Rename, Claim)
  /// attach the stored access token without any extra work from the caller.
  /// Call RefreshTokens() proactively if you want to extend the session before
  /// the access token expires (60 minutes).
  /// </summary>
  public class LeaderboardService : MonoBehaviour
  {
    [Header("Configuration")]
    [Tooltip("Responsible for the leaderboard base URL. Create via Assets → Create → UBear → LeaderboardConfig.")]
    [SerializeField] private LeaderboardConfig _config;

    private const int TimeoutSeconds = 10;

    // Public re-exports — keeps the existing call sites (UI scripts, etc.)
    // working without forcing them to know about TokenStore.
    public static string Username        => TokenStore.Username;
    public static bool   IsAuthenticated => TokenStore.IsAuthenticated;
    public static void   ClearTokens()   => TokenStore.Clear();
    public static void UpdateCachedUsername(string username) =>
      TokenStore.UpdateCachedUsername(username);

    #region  Auth Endpoints
    /// <summary>
    /// Creates a guest account and stores the returned tokens.
    /// Call this on first launch if no access token is stored.
    /// The guest account can be upgraded later via Claim().
    /// </summary>
    public IEnumerator GuestLogin(Action<ApiResult<TokenResponse>> callback)
    {
      string url = $"{_config.BaseUrl}/api/auth/guest";
      yield return Post<object, TokenResponse>(url, null, result =>
      {
        if (result.Success) TokenStore.Store(result.Data);
        callback(result);
      });
    }

    /// <summary>
    /// Ensures the client has a valid, unexpired access token before proceeding,
    /// or signals that the user needs to log in.
    ///
    /// On launch, the stored token may be hours or days old — well past the
    /// 60-minute access-token lifetime — even though it's still sitting in
    /// PlayerPrefs. 
    /// </summary>
    /// 
    /// Recovery order when the stored token isn't usable:
    ///   1. If we have a refresh token, try to rotate it. This is the common
    ///      case for a returning player whose access token aged out but whose
    ///      refresh token (7-day lifetime) is still valid. Works for both
    ///      guests and claimed users.
    ///   2. If refresh fails (or isn't possible) and the session is a guest,
    ///      create a new guest account. Guest identity is disposable; the
    ///      player should never be blocked by it.
    ///   3. If refresh fails for a CLAIMED user, do NOT silently demote them
    ///      to a fresh guest — that would orphan their identity and their
    ///      score history. Return a failed result with ErrorKind.Unauthorized
    ///      so the caller can route to a login screen.
    ///
    /// Network calls happen only when needed: a player launching within the
    /// access-token window still gets the immediate-success path.
    public IEnumerator EnsureAuthenticated(Action<ApiResult<bool>> callback)
    {
      if (TokenStore.HasUsableAccessToken())
      {
        callback(ApiResult<bool>.Ok(true));
        yield break;
      }

      // Try refresh first — preserves identity for both guests and claimed users.
      if (!string.IsNullOrEmpty(TokenStore.RefreshToken))
      {
        bool refreshed = false;
        yield return RefreshTokens(r => refreshed = r.Success);
        if (refreshed)
        {
          callback(ApiResult<bool>.Ok(true));
          yield break;
        }
        // Refresh failed. Don't ClearTokens() yet — we need IsGuestEligible()
        // below to read the stored access token's is_guest claim to decide
        // which recovery path applies. We clear later, only on the guest path.
      }

      // Refresh failed or wasn't possible. Branch on guest vs. claimed.
      if (!TokenStore.IsGuestEligible())
      {
        // Claimed user with an unrecoverable session. The UI needs to prompt
        // a real login — we deliberately do NOT clear tokens here, so a UI
        // that wants to inspect the stale identity (e.g. to pre-fill a
        // username field) still can. Logout/login flow is responsible for
        // calling ClearTokens when the user actually re-authenticates.
        callback(ApiResult<bool>.Fail(
          "Session expired. Please log in again.",
          ApiErrorKind.Unauthorized));
        yield break;
      }

      // Guest path: wipe stale tokens and create a fresh guest account so the
      // player can keep submitting scores without interruption.
      ClearTokens();
      yield return GuestLogin(result =>
      {
        callback(result.Success
          ? ApiResult<bool>.Ok(true)
          : ApiResult<bool>.Fail(result.Error, result.ErrorKind, result.StatusCode));
      });
    }

    /// <summary>
    /// Skew buffer for the JWT exp check. If a token expires within the next
    /// 30 seconds, treat it as already expired to prevent timing edge cases.
    /// </summary>
    private const int TokenExpirySkewSeconds = 30;

    /// <summary>
    /// Logs in with username and password. Stores the returned tokens.
    /// </summary>
    public IEnumerator Login(
      string username,
      string password,
      Action<ApiResult<TokenResponse>> callback)
    {
      string url = $"{_config.BaseUrl}/api/auth/login";
      var body = new LoginRequest { Username = username, Password = password };
      yield return Post<LoginRequest, TokenResponse>(url, body, result =>
      {
        if (result.Success) TokenStore.Store(result.Data);
        callback(result);
      });
    }

    /// <summary>
    /// Registers a new claimed account. Stores the returned tokens.
    /// </summary>
    public IEnumerator Register(
      string username,
      string email,
      string password,
      Action<ApiResult<TokenResponse>> callback)
    {
      string url = $"{_config.BaseUrl}/api/auth/register";
      var body = new RegisterRequest { Username = username, Email = email, Password = password };
      yield return Post<RegisterRequest, TokenResponse>(url, body, result =>
      {
        if (result.Success) TokenStore.Store(result.Data);
        callback(result);
      });
    }

    /// <summary>
    /// Rotates the stored refresh token and updates stored tokens.
    /// The old refresh token is invalidated server-side after this call.
    /// Call this proactively to extend the session before the access token expires.
    /// </summary>
    public IEnumerator RefreshTokens(Action<ApiResult<TokenResponse>> callback)
    {
      string stored = TokenStore.RefreshToken;
      if (string.IsNullOrEmpty(stored))
      {
        callback(ApiResult<TokenResponse>.Fail("No refresh token stored. Call GuestLogin() or Login() first."));
        yield break;
      }

      string url = $"{_config.BaseUrl}/api/auth/refresh";
      var body = new RefreshRequest { RefreshToken = stored };
      yield return Post<RefreshRequest, TokenResponse>(url, body, result =>
      {
        if (result.Success) TokenStore.Store(result.Data);
        callback(result);
      });
    }

    /// <summary>
    /// Revokes the stored refresh token server-side and clears local tokens.
    /// </summary>
    public IEnumerator Logout(Action<ApiResult<bool>> callback)
    {
      string stored = TokenStore.RefreshToken;
      if (string.IsNullOrEmpty(stored))
      {
        TokenStore.Clear();
        callback(ApiResult<bool>.Ok(true));
        yield break;
      }

      string url = $"{_config.BaseUrl}/api/auth/logout";
      // Note: user token provided here in body instead of header like elsewhere.
      var body = new RefreshRequest { RefreshToken = stored };

      // Logout returns 204 No Content — we parse success from the status code
      // rather than deserializing a response body. Regardless of the network
      // outcome, we clear local tokens to ensure local logout; degradation
      // means a refresh token remains valid until expiry - satisfactory here.
      yield return Post<RefreshRequest, bool>(url, body, result =>
      {
        TokenStore.Clear();
        callback(ApiResult<bool>.Ok(true));
      }, requiresAuth: false);
    }

    /// <summary>
    /// Renames the currently authenticated user.
    /// Requires a stored access token.
    /// </summary>
    public IEnumerator Rename(
      string newUsername,
      Action<ApiResult<bool>> callback)
    {
      string url = $"{_config.BaseUrl}/api/auth/rename";
      var body = new RenameRequest { Username = newUsername };
      yield return Post<RenameRequest, bool>(url, body, callback, requiresAuth: true);
    }

    /// <summary>
    /// Upgrades a guest account to a claimed account.
    /// Issues fresh tokens reflecting the claimed status.
    /// Requires a stored access token from a guest session.
    /// </summary>
    public IEnumerator Claim(
      string email,
      string password,
      Action<ApiResult<TokenResponse>> callback)
    {
      string url = $"{_config.BaseUrl}/api/auth/claim";
      var body = new ClaimRequest { Email = email, Password = password };
      yield return Post<ClaimRequest, TokenResponse>(url, body, result =>
      {
        if (result.Success) TokenStore.Store(result.Data);
        callback(result);
      }, requiresAuth: true);
    }

    #endregion
    #region  Leaderboard Endpoints

    /// <summary>
    /// Fetches the leaderboard for a given game mode and period.
    /// Period is one of: "alltime", "daily", "weekly".
    /// limit is clamped client-side to 1..100; offset is clamped to >= 0.
    /// The server enforces these ranges and returns HTTP 422 for out-of-range values.
    /// </summary>
    public IEnumerator GetScores(
        string gameMode,
        Action<ApiResult<LeaderboardResponse>> callback,
        TimePeriod period = TimePeriod.Alltime,
        int limit = 100,
        int offset = 0)
    {
      limit = Mathf.Clamp(limit, 1, 100);
      offset = Mathf.Max(offset, 0);
      string url = $"{_config.BaseUrl}/api/leaderboard/scores"
                + $"?game_mode={UnityWebRequest.EscapeURL(gameMode)}"
                + $"&period={UnityWebRequest.EscapeURL(period.ToWireValue())}"
                + $"&limit={limit}"
                + $"&offset={offset}";
      yield return Get(url, callback);
    }

    /// <summary>
    /// Fetches the most recently submitted scores across all game modes.
    /// Useful for a "recent activity" feed.
    /// limit is clamped client-side to 1..100; offset is clamped to >= 0.
    /// The server enforces these ranges and returns HTTP 422 for out-of-range values.
    /// </summary>
    public IEnumerator GetLatestScores(
      Action<ApiResult<LeaderboardResponse>> callback,
      int limit = 100,
      int offset = 0,
      string[] gameModes = null)
    {
      limit = Mathf.Clamp(limit, 1, 100);
      offset = Mathf.Max(offset, 0);
      var sb = new StringBuilder($"{_config.BaseUrl}/api/leaderboard/latest?limit={limit}&offset={offset}");
      if (gameModes != null)
      {
        foreach (string mode in gameModes)
          sb.Append($"&game_modes={UnityWebRequest.EscapeURL(mode)}");
      }
      yield return Get(sb.ToString(), callback);
    }

    /// <summary>
    /// Fetches all registered game modes and their configuration.
    /// Useful for populating a mode selector in the UI.
    /// </summary>
    public IEnumerator GetGameModes(Action<ApiResult<List<GameModeConfig>>> callback)
    {
      string url = $"{_config.BaseUrl}/api/leaderboard/game_modes";
      yield return Get(url, callback);
    }

    /// <summary>
    /// Submits a score for the authenticated user.
    /// The server upserts — if the player already has a better score,
    /// their existing record is preserved and returned.
    /// Requires a stored access token.
    /// </summary>
  public IEnumerator SubmitScore(
    long                             score,
    string                           gameMode,
    Action<ApiResult<ScoreResponse>> callback)
  {
    string url  = $"{_config.BaseUrl}/api/leaderboard/scores";
    var    body = new ScoreSubmission { Score = score, GameMode = gameMode };
    // allowGuestFallback: true — for a guest, an unrecoverable 401 must not block
    // a score submission. Falls through to a new guest account before retrying.
    // For a claimed user, the fallback is skipped at runtime (we check is_guest
    // in the JWT payload before regenerating), so claimed users still get a clean
    // 401 they can handle with a re-login prompt.
    yield return Post<ScoreSubmission, ScoreResponse>(
      url, body, callback,
      requiresAuth: true,
      allowGuestFallback: true);
  }

  #endregion
  #region  Private HTTP Helpers

  /// <summary>
  /// Generic GET — deserializes the response body into T.
  /// </summary>
  private static IEnumerator Get<T>(string url, Action<ApiResult<T>> callback)
  {
    using UnityWebRequest request = UnityWebRequest.Get(url);
    request.timeout = TimeoutSeconds;

    yield return request.SendWebRequest();

    callback(ParseResponse<T>(request));
  }

  /// <summary>
  /// Generic POST — serializes body to JSON, deserializes response into T.
  /// Pass requiresAuth: true to attach the stored Bearer token.
  /// A null body sends an empty JSON object, which is correct for
  /// endpoints like /guest that expect POST with no body.
  /// </summary>
  private IEnumerator Post<TBody, TResponse>(
    string                       url,
    TBody                        body,
    Action<ApiResult<TResponse>> callback,
    bool                         requiresAuth        = false,
    bool                         allowGuestFallback  = false)
    where TBody : class
  {
    // First attempt
    ApiResult<TResponse> firstResult = null;
    yield return SendPostOnce<TBody, TResponse>(url, body, requiresAuth, r => firstResult = r);

    if (firstResult.Success || firstResult.ErrorKind != ApiErrorKind.Unauthorized || !requiresAuth)
    {
      callback(firstResult);
      yield break;
    }

    // 401 on an authenticated request. Attempt recovery, then retry once.
    bool recovered = false;
    yield return RecoverFromUnauthorized(allowGuestFallback, r => recovered = r);

    if (!recovered)
    {
      // Recovery failed. Surface the original 401 so the caller can prompt re-auth.
      callback(firstResult);
      yield break;
    }

    // One retry with the freshly obtained token. If this also 401s, we hand the
    // result back as-is — no infinite loop, no exponential backoff. A second 401
    // means something is genuinely wrong (clock skew, server key rotation, etc.).
    ApiResult<TResponse> retryResult = null;
    yield return SendPostOnce<TBody, TResponse>(url, body, requiresAuth, r => retryResult = r);
    callback(retryResult);
  }

  /// <summary>
  /// Single POST attempt — the existing Post body factored out so the retry
  /// path can call it twice without duplicating the request construction.
  /// </summary>
  private static IEnumerator SendPostOnce<TBody, TResponse>(
    string                       url,
    TBody                        body,
    bool                         requiresAuth,
    Action<ApiResult<TResponse>> callback)
    where TBody : class
  {
    string json    = body != null ? JsonConvert.SerializeObject(body) : "{}";
    byte[] encoded = Encoding.UTF8.GetBytes(json);

    using UnityWebRequest request = new UnityWebRequest(url, "POST");
    request.uploadHandler   = new UploadHandlerRaw(encoded);
    request.downloadHandler = new DownloadHandlerBuffer();
    request.SetRequestHeader("Content-Type", "application/json");
    request.timeout = TimeoutSeconds;

    if (requiresAuth)
    {
      string token = TokenStore.AccessToken;
      if (string.IsNullOrEmpty(token))
      {
        callback(ApiResult<TResponse>.Fail(
            "No access token stored. Call GuestLogin() or Login() first.",
            ApiErrorKind.Unauthorized));
        yield break;
      }
      request.SetRequestHeader("Authorization", $"Bearer {token}");
    }

    yield return request.SendWebRequest();
    callback(ParseResponse<TResponse>(request));
  }

  /// <summary>
  /// Interprets a completed UnityWebRequest as ApiResult<T>.
  /// Network errors, HTTP errors (4xx/5xx), and JSON parse failures
  /// all surface as ApiResult.Fail with a message rather than exceptions.
  /// 204 No Content responses are treated as success with default(T).
  /// </summary>
  private static ApiResult<T> ParseResponse<T>(UnityWebRequest request)
  {
    // Network-level failure: no HTTP response at all (DNS, timeout, connection refused)
    if (request.result == UnityWebRequest.Result.ConnectionError)
    {
      return ApiResult<T>.Fail(
          $"Network error: {request.error}",
          ApiErrorKind.Network,
          statusCode: null);
    }

    long status = request.responseCode;

    // HTTP error (4xx/5xx) — UnityWebRequest reports these as ProtocolError
    if (request.result == UnityWebRequest.Result.ProtocolError)
    {
      string detail = TryExtractDetail(request.downloadHandler?.text);
      string message = string.IsNullOrEmpty(detail)
          ? $"Request failed ({status}): {request.error}"
          : $"Request failed ({status}): {detail}";

      return ApiResult<T>.Fail(message, ClassifyStatus(status), (int)status);
    }

    // Other non-success result (DataProcessingError, etc.)
    if (request.result != UnityWebRequest.Result.Success)
    {
      return ApiResult<T>.Fail(
        $"Request failed: {request.error}",
        ApiErrorKind.Network,
        statusCode: null);
    }

    // 204 No Content — success with no body to deserialize
    if (status == 204)
      return ApiResult<T>.Ok(default);

    string responseBody = request.downloadHandler.text;

    try
    {
      T data = JsonConvert.DeserializeObject<T>(responseBody);
      return ApiResult<T>.Ok(data);
    }
    catch (JsonException ex)
    {
      Debug.LogError($"[LeaderboardService] JSON parse error: {ex.Message}\nBody: {responseBody}");
      return ApiResult<T>.Fail(
        "Unexpected response format from server.",
        ApiErrorKind.ParseError,
        (int)status);
    }
  }

  private static ApiErrorKind ClassifyStatus(long status) => status switch
  {
    400 => ApiErrorKind.BadRequest,
    401 => ApiErrorKind.Unauthorized,
    403 => ApiErrorKind.Forbidden,
    404 => ApiErrorKind.NotFound,
    409 => ApiErrorKind.Conflict,
    422 => ApiErrorKind.Validation,
    429 => ApiErrorKind.RateLimited,
    >= 500 and < 600 => ApiErrorKind.Server,
    _ => ApiErrorKind.Server,
  };

  /// <summary>
  /// FastAPI wraps HTTPException errors as {"detail": "string"} and
  /// Pydantic validation errors as {"detail": [{"loc": [...], "msg": "...", ...}]}.
  /// Handle both shapes — string for HTTPException, joined msgs for validation.
  /// </summary>
  private static string TryExtractDetail(string json)
  {
    if (string.IsNullOrEmpty(json)) return null;
    try
    {
      JToken root = JToken.Parse(json);
      JToken detail = root["detail"];
      if (detail == null) return null;

      if (detail.Type == JTokenType.String)
        return detail.Value<string>();

      if (detail.Type == JTokenType.Array)
      {
        var msgs = new List<string>();
        foreach (JToken err in detail)
        {
          string msg = err["msg"]?.Value<string>();
          if (!string.IsNullOrEmpty(msg)) msgs.Add(msg);
        }
        return msgs.Count > 0 ? string.Join("; ", msgs) : null;
      }

      return detail.ToString();
    }
    catch { return null; }
  }

  /// <summary>
  /// Attempts to obtain a fresh access token after a 401, in this order:
  ///   1. Refresh using the stored refresh token (handles ordinary expiry).
  ///   2. If refresh fails and the current session is a guest (or has no
  ///      readable token at all), create a new guest account.
  ///   3. Otherwise (claimed user with an unrecoverable token), give up.
  ///
  /// Calls callback(true) if a usable access token is now stored, false otherwise.
  /// </summary>
  private IEnumerator RecoverFromUnauthorized(
    bool                    allowGuestFallback,
    Action<bool>            callback)
  {
    // Step 1: try refresh, but only if we have a refresh token to try.
    if (!string.IsNullOrEmpty(TokenStore.RefreshToken))
    {
      bool refreshed = false;
      yield return RefreshTokens(r => refreshed = r.Success);
      if (refreshed)
      {
        callback(true);
        yield break;
      }
    }

    // Step 2: refresh failed (or wasn't possible). Fall back to a new guest if
    // the caller allowed it AND the existing session is guest-eligible.
    // A claimed user with an expired token should NOT be silently demoted to a
    // new guest — that would orphan their score history and identity.
    if (!allowGuestFallback || !TokenStore.IsGuestEligible())
    {
      callback(false);
      yield break;
    }

    // Wipe stale tokens so GuestLogin starts from a clean slate.
    ClearTokens();

    bool guestOk = false;
    yield return GuestLogin(r => guestOk = r.Success);
    callback(guestOk);
  }
  #endregion
  }
}

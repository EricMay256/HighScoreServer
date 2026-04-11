// Intended to implement https://github.com/EricMay256/HighScoreServer
using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using Newtonsoft.Json;
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

        // PlayerPrefs keys — internal, not intended for callers to reference directly
        private const string PrefAccessToken  = "leaderboard_access_token";
        private const string PrefRefreshToken = "leaderboard_refresh_token";

        // ── Token access ───────────────────────────────────────────────────────

        /// <summary>
        /// Returns true if an access token is currently stored.
        /// Does not validate whether the token is still unexpired.
        /// </summary>
        public bool IsAuthenticated => !string.IsNullOrEmpty(PlayerPrefs.GetString(PrefAccessToken, null));

        /// <summary>
        /// Clears all stored tokens. Does not call /logout — use Logout() for that.
        /// </summary>
        public void ClearTokens()
        {
            PlayerPrefs.DeleteKey(PrefAccessToken);
            PlayerPrefs.DeleteKey(PrefRefreshToken);
            PlayerPrefs.Save();
        }

        private string AccessToken  => PlayerPrefs.GetString(PrefAccessToken,  null);
        private string RefreshToken => PlayerPrefs.GetString(PrefRefreshToken, null);

        private void StoreTokens(TokenResponse tokens)
        {
            PlayerPrefs.SetString(PrefAccessToken,  tokens.AccessToken);
            PlayerPrefs.SetString(PrefRefreshToken, tokens.RefreshToken);
            PlayerPrefs.Save();
        }

        // ── Auth endpoints ─────────────────────────────────────────────────────

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
                if (result.Success) StoreTokens(result.Data);
                callback(result);
            });
        }

        /// <summary>
        /// Ensures the client has a valid access token before proceeding.
        ///
        /// If a token is already stored, the callback is invoked immediately with
        /// a successful result — no network call is made. If no token exists, falls
        /// through to GuestLogin() and forwards its outcome as ApiResult&lt;bool&gt;.
        ///
        /// Use this in Start() or any entry point where you need auth before making
        /// game API calls, without caring whether the session is new or resumed.
        /// </summary>
        public IEnumerator EnsureAuthenticated(Action<ApiResult<bool>> callback)
        {
            if (IsAuthenticated)
            {
                callback(ApiResult<bool>.Ok(true));
                yield break;
            }

            yield return GuestLogin(result =>
            {
                callback(result.Success
                    ? ApiResult<bool>.Ok(true)
                    : ApiResult<bool>.Fail(result.Error));
            });
        }

        /// <summary>
        /// Logs in with username and password. Stores the returned tokens.
        /// </summary>
        public IEnumerator Login(
            string                            username,
            string                            password,
            Action<ApiResult<TokenResponse>>  callback)
        {
            string url  = $"{_config.BaseUrl}/api/auth/login";
            var    body = new LoginRequest { Username = username, Password = password };
            yield return Post<LoginRequest, TokenResponse>(url, body, result =>
            {
                if (result.Success) StoreTokens(result.Data);
                callback(result);
            });
        }

        /// <summary>
        /// Registers a new claimed account. Stores the returned tokens.
        /// </summary>
        public IEnumerator Register(
            string                           username,
            string                           email,
            string                           password,
            Action<ApiResult<TokenResponse>> callback)
        {
            string url  = $"{_config.BaseUrl}/api/auth/register";
            var    body = new RegisterRequest { Username = username, Email = email, Password = password };
            yield return Post<RegisterRequest, TokenResponse>(url, body, result =>
            {
                if (result.Success) StoreTokens(result.Data);
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
            string stored = RefreshToken;
            if (string.IsNullOrEmpty(stored))
            {
                callback(ApiResult<TokenResponse>.Fail("No refresh token stored. Call GuestLogin() or Login() first."));
                yield break;
            }

            string url  = $"{_config.BaseUrl}/api/auth/refresh";
            var    body = new RefreshRequest { RefreshToken = stored };
            yield return Post<RefreshRequest, TokenResponse>(url, body, result =>
            {
                if (result.Success) StoreTokens(result.Data);
                callback(result);
            });
        }

        /// <summary>
        /// Revokes the stored refresh token server-side and clears local tokens.
        /// </summary>
        public IEnumerator Logout(Action<ApiResult<bool>> callback)
        {
            string stored = RefreshToken;
            if (string.IsNullOrEmpty(stored))
            {
                ClearTokens();
                callback(ApiResult<bool>.Ok(true));
                yield break;
            }

            string url  = $"{_config.BaseUrl}/api/auth/logout";
            var    body = new RefreshRequest { RefreshToken = stored };

            // Logout returns 204 No Content — we parse success from the status code
            // rather than deserializing a response body.
            yield return Post<RefreshRequest, bool>(url, body, result =>
            {
                ClearTokens();
                callback(ApiResult<bool>.Ok(true));
            }, requiresAuth: false);
        }

        /// <summary>
        /// Renames the currently authenticated user.
        /// Requires a stored access token.
        /// </summary>
        public IEnumerator Rename(
            string                   newUsername,
            Action<ApiResult<bool>>  callback)
        {
            string url  = $"{_config.BaseUrl}/api/auth/rename";
            var    body = new RenameRequest { Username = newUsername };
            yield return Post<RenameRequest, bool>(url, body, callback, requiresAuth: true);
        }

        /// <summary>
        /// Upgrades a guest account to a claimed account.
        /// Issues fresh tokens reflecting the claimed status.
        /// Requires a stored access token from a guest session.
        /// </summary>
        public IEnumerator Claim(
            string                           email,
            string                           password,
            Action<ApiResult<TokenResponse>> callback)
        {
            string url  = $"{_config.BaseUrl}/api/auth/claim";
            var    body = new ClaimRequest { Email = email, Password = password };
            yield return Post<ClaimRequest, TokenResponse>(url, body, result =>
            {
                if (result.Success) StoreTokens(result.Data);
                callback(result);
            }, requiresAuth: true);
        }

        // ── Leaderboard endpoints ──────────────────────────────────────────────

        /// <summary>
        /// Fetches the leaderboard for a given game mode and period.
        /// Period is one of: "alltime", "daily", "weekly".
        /// </summary>
        public IEnumerator GetScores(
            string                                   gameMode,
            Action<ApiResult<LeaderboardResponse>>   callback,
            string                                   period = "alltime")
        {
            string url = $"{_config.BaseUrl}/api/leaderboard/scores?game_mode={gameMode}&period={period}";
            yield return Get(url, callback);
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
            int                              score,
            string                           gameMode,
            Action<ApiResult<ScoreResponse>> callback)
        {
            string url  = $"{_config.BaseUrl}/api/leaderboard/scores";
            var    body = new ScoreSubmission { Score = score, GameMode = gameMode };
            yield return Post<ScoreSubmission, ScoreResponse>(url, body, callback, requiresAuth: true);
        }

        // ── Private HTTP helpers ───────────────────────────────────────────────

        /// <summary>
        /// Generic GET — deserializes the response body into T.
        /// </summary>
        private IEnumerator Get<T>(string url, Action<ApiResult<T>> callback)
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
            bool                         requiresAuth = false)
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
                string token = AccessToken;
                if (string.IsNullOrEmpty(token))
                {
                    callback(ApiResult<TResponse>.Fail("No access token stored. Call GuestLogin() or Login() first."));
                    yield break;
                }
                request.SetRequestHeader("Authorization", $"Bearer {token}");
            }

            yield return request.SendWebRequest();

            callback(ParseResponse<TResponse>(request));
        }

        /// <summary>
        /// Interprets a completed UnityWebRequest as ApiResult&lt;T&gt;.
        /// Network errors, HTTP errors (4xx/5xx), and JSON parse failures
        /// all surface as ApiResult.Fail with a message rather than exceptions.
        /// 204 No Content responses are treated as success with default(T).
        /// </summary>
        private static ApiResult<T> ParseResponse<T>(UnityWebRequest request)
        {
            if (request.result != UnityWebRequest.Result.Success)
            {
                string serverDetail = TryExtractDetail(request.downloadHandler?.text);
                string message = string.IsNullOrEmpty(serverDetail)
                    ? $"Request failed: {request.error}"
                    : $"Request failed ({request.responseCode}): {serverDetail}";

                return ApiResult<T>.Fail(message);
            }

            // 204 No Content — success with no body to deserialize
            if (request.responseCode == 204)
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
                return ApiResult<T>.Fail("Unexpected response format from server.");
            }
        }

        /// <summary>
        /// FastAPI wraps validation/HTTP errors in {"detail": "..."}.
        /// Extract that message when available.
        /// </summary>
        private static string TryExtractDetail(string json)
        {
            if (string.IsNullOrEmpty(json)) return null;
            try
            {
                var obj = JsonConvert.DeserializeObject<Dictionary<string, string>>(json);
                obj.TryGetValue("detail", out string detail);
                return detail;
            }
            catch { return null; }
        }
    }
}
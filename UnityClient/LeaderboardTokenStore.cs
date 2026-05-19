using System;
using System.Text;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace UBear.Leaderboard
{
  /// <summary>
  /// PlayerPrefs-backed storage for leaderboard auth tokens, plus the small
  /// amount of JWT introspection the client needs to make recovery decisions.
  ///
  /// This is a static utility because PlayerPrefs is itself a process global —
  /// there is no meaningful "instance" of token storage. If you ever need to
  /// swap the backing store (secure keychain, in-memory for tests), convert
  /// this to an instance class behind an ITokenStore interface; nothing
  /// outside LeaderboardService references TokenStore's internals.
  /// </summary>
  internal static class TokenStore
  {
    // PlayerPrefs keys — internal, callers go through the properties below.
    private const string PrefAccessToken  = "leaderboard_access_token";
    private const string PrefRefreshToken = "leaderboard_refresh_token";
    private const string PrefUsername     = "leaderboard_username";

    /// <summary>
    /// Skew buffer for the JWT exp check. If a token expires within the next
    /// 30 seconds, treat it as already expired to prevent timing edge cases.
    /// </summary>
    private const int TokenExpirySkewSeconds = 30;

    public static string AccessToken  => PlayerPrefs.GetString(PrefAccessToken, null);
    public static string RefreshToken => PlayerPrefs.GetString(PrefRefreshToken, null);
    public static string Username     => PlayerPrefs.GetString(PrefUsername, null);

    /// <summary>
    /// Returns true if an access token is currently stored.
    /// Does not validate whether the token is still unexpired.
    /// </summary>
    public static bool IsAuthenticated => !string.IsNullOrEmpty(AccessToken);

    /// <summary>
    /// Persists the tokens from a successful auth response and caches the
    /// username decoded from the access token's payload.
    /// </summary>
    public static void Store(TokenResponse tokens)
    {
      PlayerPrefs.SetString(PrefAccessToken,  tokens.AccessToken);
      PlayerPrefs.SetString(PrefRefreshToken, tokens.RefreshToken);

      string username = TryDecodeJwtPayload(tokens.AccessToken)?.Value<string>("username");
      if (!string.IsNullOrEmpty(username))
        PlayerPrefs.SetString(PrefUsername, username);

      PlayerPrefs.Save();
    }

    /// <summary>
    /// Clears all stored tokens. Does not call /logout — that's the caller's job.
    /// </summary>
    public static void Clear()
    {
      PlayerPrefs.DeleteKey(PrefAccessToken);
      PlayerPrefs.DeleteKey(PrefRefreshToken);
      PlayerPrefs.DeleteKey(PrefUsername);
      PlayerPrefs.Save();
    }

    /// <summary>
    /// True if the stored access token exists and its exp claim is far enough
    /// in the future to be safely usable for the next request. Returns false
    /// for missing, expired, or unreadable tokens — all three are handled
    /// identically by the recovery path in LeaderboardService.
    ///
    /// Does NOT verify the JWT signature. A tampered token that somehow passes
    /// this exp check will fail at the server, and the 401 recovery path
    /// handles that case.
    /// </summary>
    public static bool HasUsableAccessToken()
    {
      string token = AccessToken;
      if (string.IsNullOrEmpty(token)) return false;

      JObject payload = TryDecodeJwtPayload(token);
      if (payload == null) return false;

      CacheUsernameFromPayload(payload);

      JToken expClaim = payload["exp"];
      if (expClaim == null || expClaim.Type == JTokenType.Null) return false;

      // JWT exp is seconds since Unix epoch (RFC 7519). Newtonsoft parses it
      // as a long; anything else (string, object) means the token is malformed.
      long expSeconds;
      try { expSeconds = expClaim.Value<long>(); }
      catch { return false; }

      long nowSeconds = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
      return expSeconds > nowSeconds + TokenExpirySkewSeconds;
    }

    /// <summary>
    /// True if the current stored session is a guest, or if the token is
    /// unreadable enough that we can't tell — in which case "treat as guest"
    /// is the safe choice. A claimed user with a valid-shape token returns
    /// false, which means the 401 fallback won't silently demote them.
    /// </summary>
    public static bool IsGuestEligible()
    {
      string token = AccessToken;
      if (string.IsNullOrEmpty(token)) return true;

      JObject payload = TryDecodeJwtPayload(token);
      if (payload == null) return true;

      JToken claim = payload["is_guest"];
      if (claim == null || claim.Type == JTokenType.Null) return true;

      return claim.Value<bool>();
    }

    private static void CacheUsernameFromPayload(JObject payload)
    {
      string username = payload["username"]?.Value<string>();
      if (!string.IsNullOrEmpty(username))
        PlayerPrefs.SetString(PrefUsername, username);
    }

    /// <summary>
    /// Updates the cached username without rotating tokens. Use after a successful
    /// /rename call where the server has accepted the new name but hasn't issued
    /// fresh JWTs — the stored access token's username claim is now stale, but
    /// the cache should reflect what the user sees.
    /// </summary>
    public static void UpdateCachedUsername(string username)
    {
      if (string.IsNullOrEmpty(username)) return;
      PlayerPrefs.SetString(PrefUsername, username);
      PlayerPrefs.Save();
    }

    /// <summary>
    /// Decodes the payload segment of a JWT without verifying the signature.
    /// Intentionally trust-free: the result is used only to choose a recovery
    /// strategy on the client. The server re-verifies every token on every
    /// request, so a forged payload here can only influence which fallback
    /// the client attempts — not what the server accepts.
    /// Returns null if the token is malformed.
    /// </summary>
    private static JObject TryDecodeJwtPayload(string token)
    {
      try
      {
        string[] parts = token.Split('.');
        if (parts.Length < 2) return null;

        string payload = parts[1];
        // Base64url → Base64: swap chars and re-pad.
        payload = payload.Replace('-', '+').Replace('_', '/');
        switch (payload.Length % 4)
        {
          case 2: payload += "=="; break;
          case 3: payload += "=";  break;
        }

        byte[] bytes = Convert.FromBase64String(payload);
        string json  = Encoding.UTF8.GetString(bytes);
        return JObject.Parse(json);
      }
      catch
      {
        return null;
      }
    }
  }
}
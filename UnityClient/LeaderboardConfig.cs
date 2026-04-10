using UnityEngine;

namespace UBear.Leaderboard
{
    /// <summary>
    /// Project-level leaderboard configuration.
    /// Create via Assets → Create → Leaderboard → Config.
    /// Gitignore the local dev copy as it contains sensitive information.
    /// </summary>
    [CreateAssetMenu(fileName = "LeaderboardConfig", menuName = "UBear/LeaderboardConfig")]
    public class LeaderboardConfig : ScriptableObject
    {
        [Tooltip("Base URL of the leaderboard server, no trailing slash.")]
        public string BaseUrl = "https://your-app.herokuapp.com";

        [Tooltip("Stored JWT access token. Populated at runtime — do not set manually.")]
        public string AccessToken = "";

        [Tooltip("Stored refresh token. Populated at runtime — do not set manually.")]
        public string RefreshToken = "";
    }
}
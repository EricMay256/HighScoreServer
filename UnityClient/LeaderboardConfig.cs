using UnityEngine;

namespace UBear.Leaderboard
{
    /// <summary>
    /// Project-level leaderboard configuration.
    /// Create via Assets → Create → UBear → LeaderboardConfig.
    /// Gitignore the local dev copy as it contains sensitive information.
    /// </summary>
    [CreateAssetMenu(fileName = "LeaderboardConfig", menuName = "UBear/LeaderboardConfig")]
    public class LeaderboardConfig : ScriptableObject
    {
        [Tooltip("Base URL of the leaderboard server, no trailing slash.")]
        public string BaseUrl = "https://your-app.herokuapp.com";
        [Tooltip("Game modes this client cares about. Used to scope /latest queries.")]
        //This is currently the only way games "claim" game modes
        //See README "Known Future Considerations" for deferred server-side ownership option
        public string[] GameModes;
    }
}
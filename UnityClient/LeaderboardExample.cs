using System.Collections;
using System.Collections.Generic;
using UnityEngine;

namespace UBear.Leaderboard
{
    /// <summary>
    /// Minimal example showing how to use LeaderboardService.
    /// Attach both this and LeaderboardService to the same GameObject,
    /// or reference LeaderboardService from a known scene object.
    /// </summary>
    public class LeaderboardExample : MonoBehaviour
    {
        [SerializeField] private LeaderboardService _service;

        private void Start()
        {
            StartCoroutine(_service.EnsureAuthenticated(OnAuthenticated));
        }

        private void OnAuthenticated(ApiResult<bool> result)
        {
            if (!result.Success)
            {
                Debug.LogWarning($"[Leaderboard] Auth failed: {result.Error}");
                // Show error UI here
                return;
            }
            Debug.Log("[Leaderboard] Authenticated successfully.");
            StartCoroutine(_service.GetScores("classic", OnScoresReceived));
        }

        /// <summary>
        /// Example method showing how to submit a score. Call this from a UI button, game-over screen, etc.
        /// No arguments required - please ensure calls are not being made more frequently than intended.
        /// </summary>
        public void SubmitButton()
        {
            StartCoroutine(_service.SubmitScore(Random.Range(0, 100), "classic", OnScoreSubmitted));
        }

        // Call this from a UI button, game-over screen, etc.
        public void SubmitPlayerScore(string playerName, int score)
        {
            StartCoroutine(_service.SubmitScore(score, "classic", OnScoreSubmitted));
        }

        // ── Callbacks ──────────────────────────────────────────────────────────

        private void OnScoresReceived(ApiResult<LeaderboardResponse> result)
        {
            if (!result.Success)
            {
                Debug.LogWarning($"[Leaderboard] Failed to load scores: {result.Error}");
                // Show error UI here
                return;
            }

            Debug.Log($"Total players: {result.Data.TotalCount}");
            foreach (ScoreResponse entry in result.Data.Scores)
            {
                Debug.Log($"#{entry.Rank} {entry.Player}: {entry.Score} ({entry.Percentile}%)");
            }

            // Populate your UI here with result.Data
        }

        private void OnScoreSubmitted(ApiResult<ScoreResponse> result)
        {
            if (!result.Success)
            {
                Debug.LogWarning($"[Leaderboard] Score submission failed: {result.Error}");
                return;
            }

            Debug.Log($"Score accepted. Current best: {result.Data.Score}");
        }
    }
}
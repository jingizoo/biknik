/* Initial Setup login integration (#174 PR D).

   app.js intentionally moves a signed-out visitor from the operator Dashboard to
   public Standings. Preserve that safety posture, but when a fresh authenticated
   League Admin session is established, restore the untouched operator landing to
   Dashboard so onboarding.js can make its one-time server-derived route decision.
   Explicit in-session tab choices and persona switches are not changed. */

const onboardingIntegratedSetUser = setUser;
setUser = function setUserWithInitialSetupLanding(user) {
  const wasSignedOut = !currentUser && document.body.classList.contains("signed-out");
  onboardingIntegratedSetUser(user);
  if (wasSignedOut && currentUser && currentUser.role === "league_admin"
      && view === "standings") {
    view = "dashboard";
    onboardingRoutePending = true;
  }
};

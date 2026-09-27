// Copy this file to firebase-config.js (same folder) and fill in the values
// from your Firebase project settings (Project settings -> General ->
// Your apps -> Web app -> SDK setup and configuration -> Config).
// These values are NOT secret - Firebase's own security model expects them
// to be public in client-side code; real access control is done by the
// Realtime Database security rules, not by hiding this object.
window.FIREBASE_CONFIG = {
  apiKey: "PASTE_API_KEY",
  authDomain: "PASTE_PROJECT_ID.firebaseapp.com",
  databaseURL: "https://PASTE_PROJECT_ID-default-rtdb.firebaseio.com",
  projectId: "PASTE_PROJECT_ID",
  storageBucket: "PASTE_PROJECT_ID.appspot.com",
  messagingSenderId: "PASTE_SENDER_ID",
  appId: "PASTE_APP_ID",
};

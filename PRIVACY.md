# Privacy Policy

**E1001-Calendar** is a personal, non-commercial project. It is used only by its developer, to show reminders on a home e-paper display. It is not offered to the public, and there are no other users.

Last updated: November 23, 2026

## What Google data the app accesses

When the developer signs in, the app asks for one permission: **read-only access to Google Tasks** (`https://www.googleapis.com/auth/tasks.readonly`). It uses this to read the developer's own task titles and due dates. It cannot create, change or delete tasks, and it does not access Gmail, Drive, Calendar, contacts or any other Google data.

## How the data is used

Task titles and due dates are used only to draw the "Reminders" strip on the developer's own display. They are not used for advertising, analytics or profiling, and they are not used to train any AI or machine-learning model. No person looks at the data.

## Storage and sharing

- Tasks are fetched on demand and kept only in memory, on a private Raspberry Pi in the developer's home.
- The OAuth refresh token is stored on that same Pi, in a file that is not part of this repository.
- Data is never sold, shared with or transferred to any third party, and is not sent anywhere other than the developer's own display.

## Google API Services User Data Policy

The app's use and transfer of information received from Google APIs adheres to the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy#limited-use), including the Limited Use requirements.

## Retention, revoking access and deletion

Task data is not kept after a screen is drawn. Access can be revoked at any time at [myaccount.google.com/permissions](https://myaccount.google.com/permissions). Deleting the refresh token from the Pi also removes the app's access.

## Contact

Questions: shields.sims@gmail.com

#ifndef GAME_PLAYER_H
#define GAME_PLAYER_H

#include "Common.h"
#include "net/Session.h"
#include <vector>

namespace Game {

/**
 * @brief A connected player character.
 *
 * Owns the session it was created for.
 */
class Player
{
    public:
        explicit Player(Session* session) : m_session(session) {}
        ~Player();

        /// Advances timers by `diff` milliseconds.
        void Update(uint32 diff);
        bool IsAlive() const { return m_health > 0; }

        struct Stats
        {
            int strength;
        };

    private:
        Session* m_session;
        int m_health = 100;
};

enum class Team { Alliance, Horde };

template <typename T>
T Clamp(T value, T lo, T hi) { return value < lo ? lo : (value > hi ? hi : value); }

} // namespace Game

#endif
